from __future__ import annotations

import base64
from collections import OrderedDict
from concurrent.futures import (
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeout,
    wait,
)
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from typing import Any

import httpx
from openai import OpenAI

from app.config import Settings
from app.conversation_store import SavedConversationStore
from app.datastore import BaseDocumentStore, RetrievalContext
from app.document_generator import ContextDocumentGenerator, GeneratedDocumentResult
from app.models import (
    ChatImage,
    ChatResponse,
    Citation,
    ContextFilter,
    ConversationMessage,
    GeneratedChatDocument,
    ReasoningMode,
    SavedConversationDetail,
    SavedConversationSummary,
    SessionState,
    SourceMode,
    ToolTrace,
)
from app.prompts import build_context_scope_prompt, build_system_prompt
from app.reasoning_profiles import get_chat_reasoning_profile
from app.sensitive_text import redact_sensitive_text
from app.source_retrieval import SourceDocumentRetriever


chat_logger = logging.getLogger("uvicorn.error")
_CHAT_MODEL_EXECUTOR = ThreadPoolExecutor(
    max_workers=16,
    thread_name_prefix="askjenny-model",
)
_DATASHEET_BATCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="askjenny-datasheet",
)
_DATASHEET_PRODUCT_RE = re.compile(
    r"\b(?=[A-Z0-9-]*\d)[A-Z0-9]+(?:-[A-Z0-9]+)+\b",
    flags=re.IGNORECASE,
)
_MAX_DATASHEET_BATCH_PRODUCTS = 8


class ChatCancelledError(RuntimeError):
    pass


class ChatDeadlineError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatasheetRetrievalResult:
    product: str
    document: GeneratedChatDocument | None
    citation: Citation | None
    detail: str


TOOLS = [
    {
        "type": "function",
        "name": "search_documents",
        "description": (
            "Search processed internal business documents. Use this before answering "
            "questions about company knowledge, SOPs, billing, support, onboarding, "
            "security, or other internal facts."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query to run against the internal document store.",
                },
                "limit": {
                    "type": "integer",
                    "description": "The maximum number of document hits to return.",
                    "minimum": 1,
                    "maximum": 8,
                },
            },
            "required": ["query", "limit"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_document",
        "description": "Fetch a specific internal business document by document ID.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "The exact internal document identifier to load.",
                }
            },
            "required": ["document_id"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "generate_context_document",
        "description": (
            "Create a downloadable document from the current conversation and internal library context. "
            "Use this when the user wants a file or deliverable created, such as a report, draft, template, "
            "letter, memo, spreadsheet, checklist, or structured export. Choose the best output format "
            "yourself: use pdf for polished read-only documents, docx for editable formal documents, "
            "xlsx for spreadsheets/tables/trackers, and txt for plain text."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": ["string", "null"],
                    "description": "A concise, user-facing file title chosen from the conversation intent.",
                },
                "output_format": {
                    "type": "string",
                    "enum": ["txt", "docx", "pdf", "xlsx"],
                    "description": "The most appropriate downloadable format for the requested deliverable.",
                },
                "instructions": {
                    "type": "string",
                    "description": (
                        "The document-generation brief inferred from the conversation, including structure, audience, "
                        "and requested contents."
                    ),
                },
            },
            "required": ["title", "output_format", "instructions"],
            "additionalProperties": False,
        },
    },
]
WEB_SEARCH_TOOL = {"type": "web_search"}
SOURCE_RETRIEVAL_TOOL = {
    "type": "function",
    "name": "retrieve_source_pdf",
    "description": (
        "Download the original public PDF source document from a direct official HTTPS URL. "
        "Use this after web search when the user asks for a datasheet, manual, specification, "
        "or other source PDF. This attaches the untouched PDF to the private conversation and "
        "does not add it to the embedded document library."
    ),
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The direct public HTTPS URL of the original PDF.",
            },
            "filename": {
                "type": ["string", "null"],
                "description": "A concise filename ending in .pdf, if known.",
            },
            "title": {
                "type": ["string", "null"],
                "description": "The human-readable source document title.",
            },
        },
        "required": ["url", "filename", "title"],
        "additionalProperties": False,
    },
}

INLINE_CITATION_RE = re.compile(r"\[([A-Za-z0-9][A-Za-z0-9_-]*)\]")
GENERATED_UPLOAD_CITATION_RE = re.compile(
    r"[ \t]*\[UPL-[A-Za-z0-9][A-Za-z0-9_-]*\]",
    flags=re.IGNORECASE,
)


def strip_generated_upload_citations(message: str) -> str:
    cleaned = GENERATED_UPLOAD_CITATION_RE.sub("", str(message or ""))
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


class SessionManager:
    def __init__(
        self,
        ttl_minutes: int,
        saved_conversations: SavedConversationStore | None = None,
    ) -> None:
        self._ttl = timedelta(minutes=ttl_minutes)
        self._saved_conversations = saved_conversations
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    def get_or_create(
        self,
        conversation_id: str | None,
        owner_user_id: str,
    ) -> SessionState:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._prune_locked(now)
            session_id = conversation_id or str(uuid.uuid4())
            session = self._sessions.get(session_id)
            if session is not None and session.owner_user_id != owner_user_id:
                raise PermissionError("Conversation belongs to another user.")
            if session is None:
                session = self._load_saved_session_locked(
                    session_id,
                    owner_user_id,
                    now,
                )
                if session is None:
                    saved_owner = (
                        self._saved_conversations.get_conversation_owner(session_id)
                        if self._saved_conversations is not None
                        else None
                    )
                    if saved_owner is not None and saved_owner != owner_user_id:
                        raise PermissionError("Conversation belongs to another user.")
                    session = SessionState(
                        conversation_id=session_id,
                        owner_user_id=owner_user_id,
                    )
                self._sessions[session_id] = session
            session.last_touched = now
            return session

    def list_saved_conversations(
        self,
        owner_user_id: str,
    ) -> list[SavedConversationSummary]:
        if self._saved_conversations is None:
            return []
        return self._saved_conversations.list_conversations(owner_user_id)

    def load_saved_conversation(
        self,
        conversation_id: str,
        owner_user_id: str,
    ) -> SavedConversationDetail | None:
        if self._saved_conversations is None:
            return None
        conversation = self._saved_conversations.get_conversation(
            conversation_id,
            owner_user_id,
        )
        if conversation is None:
            return None

        now = datetime.now(timezone.utc)
        with self._lock:
            self._prune_locked(now)
            self._sessions[conversation_id] = self._session_from_saved(conversation, now)
        return conversation

    def save_conversation(
        self,
        conversation_id: str,
        owner_user_id: str,
        title: str | None = None,
        source_mode: SourceMode | None = None,
        reasoning_mode: ReasoningMode | None = None,
        context_filter: ContextFilter | None = None,
    ) -> SavedConversationDetail:
        if self._saved_conversations is None:
            raise RuntimeError("Saved conversation storage is not configured.")
        session = self.get_or_create(conversation_id, owner_user_id)
        if source_mode is not None:
            session.source_mode = source_mode
        if reasoning_mode is not None:
            session.reasoning_mode = reasoning_mode
        if context_filter is not None:
            session.context_filter = context_filter.model_copy(deep=True)
        return self._saved_conversations.save_session(session, title=title)

    def update_conversation_settings(
        self,
        conversation_id: str,
        owner_user_id: str,
        source_mode: SourceMode,
        context_filter: ContextFilter,
        reasoning_mode: ReasoningMode = "standard",
    ) -> SavedConversationDetail | None:
        session = self.get_or_create(conversation_id, owner_user_id)
        session.source_mode = source_mode
        session.reasoning_mode = reasoning_mode
        session.context_filter = context_filter.model_copy(deep=True)

        if self._saved_conversations is None:
            return None
        if self._saved_conversations.get_conversation(
            conversation_id,
            owner_user_id,
        ) is None:
            return None
        return self._saved_conversations.save_session(session)

    def delete_saved_conversation(
        self,
        conversation_id: str,
        owner_user_id: str,
    ) -> bool:
        if self._saved_conversations is None:
            raise RuntimeError("Saved conversation storage is not configured.")
        return self._saved_conversations.delete_conversation(
            conversation_id,
            owner_user_id,
        )

    def delete_saved_conversation_pair(
        self,
        conversation_id: str,
        assistant_message_index: int,
        owner_user_id: str,
    ) -> SavedConversationDetail | None:
        if self._saved_conversations is None:
            raise RuntimeError("Saved conversation storage is not configured.")

        conversation = self._saved_conversations.delete_message_pair(
            conversation_id,
            assistant_message_index,
            owner_user_id,
        )
        if conversation is None:
            return None

        now = datetime.now(timezone.utc)
        with self._lock:
            self._prune_locked(now)
            self._sessions[conversation_id] = self._session_from_saved(conversation, now)
        return conversation

    def _prune_locked(self, now: datetime) -> None:
        expired = [
            key
            for key, session in self._sessions.items()
            if now - session.last_touched > self._ttl
        ]
        for key in expired:
            del self._sessions[key]

    def _load_saved_session_locked(
        self,
        conversation_id: str,
        owner_user_id: str,
        now: datetime,
    ) -> SessionState | None:
        if self._saved_conversations is None:
            return None
        conversation = self._saved_conversations.get_conversation(
            conversation_id,
            owner_user_id,
        )
        if conversation is None:
            return None
        return self._session_from_saved(conversation, now)

    def _session_from_saved(
        self,
        conversation: SavedConversationDetail,
        now: datetime,
    ) -> SessionState:
        return SessionState(
            conversation_id=conversation.conversation_id,
            owner_user_id=conversation.owner_user_id,
            history=self._build_history_from_messages(conversation.messages),
            transcript=[
                ConversationMessage.model_validate(message.model_dump())
                for message in conversation.messages
            ],
            source_mode=conversation.source_mode,
            reasoning_mode=conversation.reasoning_mode,
            context_filter=conversation.context_filter.model_copy(deep=True),
            created_at=datetime.fromisoformat(conversation.created_at),
            last_touched=now,
        )

    def _build_history_from_messages(self, messages: list[ConversationMessage]) -> list[Any]:
        history: list[Any] = []
        for message in messages:
            if message.role not in {"user", "assistant"}:
                continue
            if message.role == "user" and message.images:
                content: list[dict[str, str]] = []
                if message.body:
                    content.append({"type": "input_text", "text": message.body})
                else:
                    content.append(
                        {
                            "type": "input_text",
                            "text": "Please analyze the attached image or images.",
                        }
                    )
                content.extend(
                    {
                        "type": "input_image",
                        "image_url": (
                            f"data:{image.mime_type};base64,{image.content_base64}"
                        ),
                    }
                    for image in message.images
                )
                history.append({"role": "user", "content": content})
                continue
            history.append({"role": message.role, "content": message.body})
        return history


class BusinessKnowledgeAgent:
    def __init__(
        self,
        settings: Settings,
        document_store: BaseDocumentStore,
        sessions: SessionManager,
        document_generator: ContextDocumentGenerator | None = None,
    ) -> None:
        self._settings = settings
        self._document_store = document_store
        self._sessions = sessions
        self._document_generator = document_generator
        self._source_retriever = SourceDocumentRetriever(
            timeout_seconds=settings.source_download_timeout_seconds,
            max_bytes=settings.source_download_max_bytes,
        )
        self._active_request_lock = threading.Lock()
        self._active_requests: dict[
            str,
            tuple[str, threading.Event, list[OpenAI]],
        ] = {}
        self._client: OpenAI | None = None

    @property
    def client(self) -> OpenAI:
        if not self._settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured.")
        if self._client is None:
            self._client = OpenAI(
                api_key=self._settings.openai_api_key,
                timeout=max(1, self._settings.openai_request_timeout_seconds),
                max_retries=0,
            )
        return self._client

    def _create_openai_client(self) -> OpenAI:
        if not self._settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured.")
        return OpenAI(
            api_key=self._settings.openai_api_key,
            timeout=max(1, self._settings.openai_request_timeout_seconds),
            max_retries=0,
        )

    def chat(
        self,
        conversation_id: str | None,
        owner_user_id: str,
        message: str,
        images: list[ChatImage],
        source_mode: SourceMode,
        context_filter: ContextFilter,
        reasoning_mode: ReasoningMode = "standard",
        request_id: str | None = None,
    ) -> ChatResponse:
        cancellation = threading.Event()
        normalized_request_id = str(request_id or "").strip()
        request_client = self._client or self._create_openai_client()
        owns_request_client = self._client is None
        request_clients = [request_client]
        rollback_session = (
            self._sessions.get_or_create(conversation_id, owner_user_id)
            if conversation_id
            else None
        )
        rollback_history_length = (
            len(rollback_session.history) if rollback_session is not None else 0
        )
        rollback_transcript_length = (
            len(rollback_session.transcript) if rollback_session is not None else 0
        )
        if normalized_request_id:
            with self._active_request_lock:
                self._active_requests[normalized_request_id] = (
                    owner_user_id,
                    cancellation,
                    request_clients,
                )
        try:
            response = self._chat_impl(
                conversation_id,
                owner_user_id,
                message,
                images,
                source_mode,
                context_filter,
                reasoning_mode,
                normalized_request_id,
                request_client,
                request_clients,
                cancellation,
            )
            chat_logger.info(
                "Chat finished: request_id=%s conversation_id=%s",
                normalized_request_id or "untracked",
                response.conversation_id,
            )
            return response
        except ChatCancelledError:
            if rollback_session is not None:
                del rollback_session.history[rollback_history_length:]
                del rollback_session.transcript[rollback_transcript_length:]
            chat_logger.warning(
                "Chat cancelled: request_id=%s conversation_id=%s",
                normalized_request_id or "untracked",
                conversation_id or "new",
            )
            raise
        finally:
            if normalized_request_id:
                with self._active_request_lock:
                    active = self._active_requests.get(normalized_request_id)
                    if active is not None and active[1] is cancellation:
                        self._active_requests.pop(normalized_request_id, None)
            clients_to_close = request_clients if owns_request_client else request_clients[1:]
            for client_to_close in clients_to_close:
                try:
                    client_to_close.close()
                except Exception:
                    chat_logger.exception(
                        "Could not close request client: request_id=%s",
                        normalized_request_id or "untracked",
                    )

    def cancel_request(self, request_id: str, owner_user_id: str) -> bool:
        with self._active_request_lock:
            active = self._active_requests.get(str(request_id or "").strip())
            if active is None or active[0] != owner_user_id:
                return False
            active[1].set()
            request_clients = list(active[2])
        for request_client in request_clients:
            try:
                request_client.close()
            except Exception:
                chat_logger.exception(
                    "Could not interrupt cancelled model request: request_id=%s",
                    request_id,
                )
        chat_logger.info("Chat cancellation requested: request_id=%s", request_id)
        return True

    def _chat_impl(
        self,
        conversation_id: str | None,
        owner_user_id: str,
        message: str,
        images: list[ChatImage],
        source_mode: SourceMode,
        context_filter: ContextFilter,
        reasoning_mode: ReasoningMode,
        request_id: str,
        request_client: OpenAI,
        request_clients: list[OpenAI],
        cancellation: threading.Event,
    ) -> ChatResponse:
        session = self._sessions.get_or_create(conversation_id, owner_user_id)
        session.source_mode = source_mode
        session.reasoning_mode = reasoning_mode
        session.context_filter = context_filter.model_copy(deep=True)
        user_content: list[dict[str, str]] = []
        if message:
            user_content.append({"type": "input_text", "text": message})
        elif images:
            user_content.append(
                {
                    "type": "input_text",
                    "text": "Please analyze the attached image or images.",
                }
            )
        user_content.extend(
            {
                "type": "input_image",
                "image_url": f"data:{image.mime_type};base64,{image.content_base64}",
            }
            for image in images
        )
        session.history.append({"role": "user", "content": user_content})
        session.transcript.append(
            ConversationMessage(
                role="user",
                label="You",
                body=message,
                images=[image.model_copy(deep=True) for image in images],
            )
        )
        batch_products = self._extract_datasheet_products(message)
        if source_mode == "broader" and len(batch_products) >= 2:
            return self._retrieve_datasheet_batch(
                session,
                batch_products,
                reasoning_mode,
                context_filter,
                request_id,
                request_client,
                request_clients,
                cancellation,
            )
        retrieval_context = RetrievalContext.from_lists(
            folder_ids=context_filter.folder_ids,
            document_ids=context_filter.document_ids,
        )

        citations: "OrderedDict[str, Citation]" = OrderedDict()
        traces: list[ToolTrace] = []
        generated_document: GeneratedChatDocument | None = None
        generated_documents: list[GeneratedChatDocument] = []
        started_at = time.monotonic()
        deadline = started_at + max(1, self._settings.chat_request_timeout_seconds)
        max_rounds = max(1, self._settings.chat_max_tool_rounds)
        max_source_attempts = max(1, self._settings.source_download_max_attempts)
        round_count = 0
        source_attempt_count = 0

        def check_cancelled() -> None:
            if cancellation.is_set():
                raise ChatCancelledError("Response cancelled by the user.")

        chat_logger.info(
            "Chat started: request_id=%s conversation_id=%s mode=%s timeout=%ss max_rounds=%s",
            request_id or "untracked",
            session.conversation_id,
            source_mode,
            self._settings.chat_request_timeout_seconds,
            max_rounds,
        )

        while True:
            check_cancelled()
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                return self._finish_limited_response(
                    session,
                    citations,
                    traces,
                    generated_document,
                    generated_documents,
                    source_mode,
                    reasoning_mode,
                    context_filter,
                    (
                        "I stopped this response because it reached the time limit. "
                        "Please try again with a specific manufacturer, product number, "
                        "or direct source link."
                    ),
                )
            if round_count >= max_rounds:
                return self._finish_limited_response(
                    session,
                    citations,
                    traces,
                    generated_document,
                    generated_documents,
                    source_mode,
                    reasoning_mode,
                    context_filter,
                    (
                        "I stopped after the maximum number of search and retrieval steps "
                        "to avoid getting stuck. Please provide a more specific product "
                        "number or direct source link."
                    ),
                )
            round_count += 1
            model_timeout = min(
                remaining_seconds,
                max(1, self._settings.openai_request_timeout_seconds),
            )
            chat_logger.info(
                "Chat model round started: request_id=%s round=%s/%s timeout=%.1fs",
                request_id or "untracked",
                round_count,
                max_rounds,
                model_timeout,
            )
            try:
                response = self._run_response_with_controls(
                    session.history,
                    source_mode,
                    retrieval_context,
                    reasoning_mode,
                    request_client,
                    check_cancelled,
                    deadline,
                    model_timeout,
                )
            except ChatDeadlineError:
                return self._finish_limited_response(
                    session,
                    citations,
                    traces,
                    generated_document,
                    generated_documents,
                    source_mode,
                    reasoning_mode,
                    context_filter,
                    (
                        "I stopped this response because it reached the time limit. "
                        "Please try again with a specific manufacturer, product number, "
                        "or direct source link."
                    ),
                )
            check_cancelled()
            session.history.extend(response.output)

            web_citations, web_traces = self._collect_web_search_metadata(response.output)
            for citation in web_citations:
                citations[citation.document_id] = citation
            traces.extend(web_traces)

            tool_calls = [item for item in response.output if getattr(item, "type", None) == "function_call"]
            if not tool_calls:
                check_cancelled()
                raw_assistant_message = response.output_text.strip()
                if not raw_assistant_message:
                    raw_assistant_message = "I could not produce a final answer."
                final_citations = self._select_final_citations(
                    assistant_message=raw_assistant_message,
                    citations=citations,
                    traces=traces,
                )
                assistant_message = (
                    strip_generated_upload_citations(raw_assistant_message)
                    or "I could not produce a final answer."
                )
                session.transcript.append(
                    ConversationMessage(
                        role="assistant",
                        label="Assistant",
                        body=assistant_message,
                        citations=final_citations,
                        tool_trace=traces,
                        generated_document=generated_document,
                        generated_documents=generated_documents,
                    )
                )
                return ChatResponse(
                    conversation_id=session.conversation_id,
                    assistant_message=assistant_message,
                    citations=final_citations,
                    tool_trace=traces,
                    generated_document=generated_document,
                    generated_documents=generated_documents,
                    source_mode=source_mode,
                    reasoning_mode=reasoning_mode,
                    context_filter=context_filter,
                )

            for tool_call in tool_calls:
                check_cancelled()
                arguments = json.loads(tool_call.arguments)
                if tool_call.name == "retrieve_source_pdf":
                    source_attempt_count += 1
                if (
                    tool_call.name == "retrieve_source_pdf"
                    and source_attempt_count > max_source_attempts
                ):
                    summary = (
                        "Skipped another source PDF attempt because this response already "
                        "used its download attempt."
                    )
                    tool_output = {
                        "retrieved": False,
                        "url": arguments.get("url"),
                        "message": summary,
                    }
                    new_citations = []
                    generated_result = None
                    chat_logger.warning(
                        "Source PDF attempt blocked: request_id=%s attempt=%s limit=%s",
                        request_id or "untracked",
                        source_attempt_count,
                        max_source_attempts,
                    )
                else:
                    chat_logger.info(
                        "Chat tool started: request_id=%s tool=%s",
                        request_id or "untracked",
                        tool_call.name,
                    )
                    tool_output, new_citations, summary, generated_result = self._execute_tool(
                        tool_call.name,
                        arguments,
                        source_mode,
                        retrieval_context,
                        reasoning_mode,
                        cancel_check=check_cancelled,
                    )
                    chat_logger.info(
                        "Chat tool finished: request_id=%s tool=%s summary=%s",
                        request_id or "untracked",
                        tool_call.name,
                        summary,
                    )
                for citation in new_citations:
                    citations[citation.document_id] = citation
                if generated_result is not None:
                    is_source_document = tool_call.name == "retrieve_source_pdf"
                    source_url = (
                        generated_result.citations[0].source_url
                        if is_source_document and generated_result.citations
                        else None
                    )
                    generated_document = GeneratedChatDocument(
                        filename=generated_result.filename,
                        mime_type=generated_result.mime_type,
                        content_base64=base64.b64encode(generated_result.content_bytes).decode("ascii"),
                        title=arguments.get("title"),
                        document_kind="source" if is_source_document else "generated",
                        source_url=source_url,
                    )
                    generated_documents.append(generated_document)
                traces.append(
                    ToolTrace(
                        tool_name=tool_call.name,
                        arguments=arguments,
                        summary=summary,
                    )
                )
                session.history.append(
                    {
                        "type": "function_call_output",
                        "call_id": tool_call.call_id,
                        "output": json.dumps(tool_output, ensure_ascii=True),
                    }
                )

    @staticmethod
    def _extract_datasheet_products(message: str) -> list[str]:
        normalized_message = str(message or "")
        if not re.search(r"\bdata\s*sheet(?:s)?\b", normalized_message, flags=re.IGNORECASE):
            return []
        products: list[str] = []
        seen: set[str] = set()
        for match in _DATASHEET_PRODUCT_RE.finditer(normalized_message):
            product = match.group(0).upper()
            if product in seen:
                continue
            seen.add(product)
            products.append(product)
            if len(products) >= _MAX_DATASHEET_BATCH_PRODUCTS:
                break
        return products

    def _retrieve_datasheet_batch(
        self,
        session: SessionState,
        products: list[str],
        reasoning_mode: ReasoningMode,
        context_filter: ContextFilter,
        request_id: str,
        request_client: OpenAI,
        request_clients: list[OpenAI],
        cancellation: threading.Event,
    ) -> ChatResponse:
        deadline = time.monotonic() + max(1, self._settings.chat_request_timeout_seconds)

        def check_cancelled() -> None:
            if cancellation.is_set():
                raise ChatCancelledError("Response cancelled by the user.")

        clients_by_product: dict[str, OpenAI] = {products[0]: request_client}
        for product in products[1:]:
            check_cancelled()
            product_client = self._create_openai_client()
            clients_by_product[product] = product_client
            with self._active_request_lock:
                request_clients.append(product_client)

        chat_logger.info(
            "Datasheet batch started: request_id=%s products=%s",
            request_id or "untracked",
            ",".join(products),
        )
        futures = {
            _DATASHEET_BATCH_EXECUTOR.submit(
                self._retrieve_single_datasheet,
                product,
                reasoning_mode,
                clients_by_product[product],
                cancellation,
                deadline,
            ): product
            for product in products
        }
        pending = set(futures)
        results_by_product: dict[str, DatasheetRetrievalResult] = {}
        while pending:
            check_cancelled()
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                for pending_future in pending:
                    pending_future.cancel()
                break
            completed, pending = wait(
                pending,
                timeout=min(0.1, remaining_seconds),
                return_when=FIRST_COMPLETED,
            )
            for completed_future in completed:
                product = futures[completed_future]
                try:
                    results_by_product[product] = completed_future.result()
                except ChatCancelledError:
                    raise
                except Exception as exc:
                    chat_logger.exception(
                        "Datasheet product retrieval failed: request_id=%s product=%s",
                        request_id or "untracked",
                        product,
                    )
                    results_by_product[product] = DatasheetRetrievalResult(
                        product=product,
                        document=None,
                        citation=None,
                        detail=f"Retrieval failed: {type(exc).__name__}",
                    )

        for pending_future in pending:
            product = futures[pending_future]
            results_by_product[product] = DatasheetRetrievalResult(
                product=product,
                document=None,
                citation=None,
                detail="Timed out before retrieval completed",
            )

        ordered_results = [
            results_by_product.get(
                product,
                DatasheetRetrievalResult(
                    product=product,
                    document=None,
                    citation=None,
                    detail="No retrieval result",
                ),
            )
            for product in products
        ]
        documents = [
            result.document for result in ordered_results if result.document is not None
        ]
        citations = [
            result.citation for result in ordered_results if result.citation is not None
        ]
        traces = [
            ToolTrace(
                tool_name="retrieve_source_pdf",
                arguments={"product": result.product},
                summary=(
                    f"Retrieved {result.document.filename}."
                    if result.document is not None
                    else result.detail
                ),
            )
            for result in ordered_results
        ]
        table_rows = [
            (
                f"| {result.product} | "
                f"{'Retrieved' if result.document is not None else result.detail.replace('|', '/')} |"
            )
            for result in ordered_results
        ]
        assistant_message = (
            f"I retrieved {len(documents)} of {len(products)} requested datasheets. "
            "Each successful PDF is attached separately and was not added to the library.\n\n"
            "| Product | Result |\n"
            "|---|---|\n"
            + "\n".join(table_rows)
        )
        session.transcript.append(
            ConversationMessage(
                role="assistant",
                label="Assistant",
                body=assistant_message,
                citations=citations,
                tool_trace=traces,
                generated_document=documents[0] if documents else None,
                generated_documents=documents,
            )
        )
        chat_logger.info(
            "Datasheet batch finished: request_id=%s retrieved=%s requested=%s",
            request_id or "untracked",
            len(documents),
            len(products),
        )
        return ChatResponse(
            conversation_id=session.conversation_id,
            assistant_message=assistant_message,
            citations=citations,
            tool_trace=traces,
            generated_document=documents[0] if documents else None,
            generated_documents=documents,
            source_mode="broader",
            reasoning_mode=reasoning_mode,
            context_filter=context_filter,
        )

    def _retrieve_single_datasheet(
        self,
        product: str,
        reasoning_mode: ReasoningMode,
        client: OpenAI,
        cancellation: threading.Event,
        deadline: float,
    ) -> DatasheetRetrievalResult:
        def check_cancelled() -> None:
            if cancellation.is_set():
                raise ChatCancelledError("Response cancelled by the user.")

        history: list[Any] = [
            {
                "role": "user",
                "content": (
                    f"Find the exact original datasheet PDF for product model {product}. "
                    "Search the official manufacturer first, use a direct public HTTPS PDF URL, "
                    "and call retrieve_source_pdf exactly once. Do not return a similar model."
                ),
            }
        ]
        retrieval_context = RetrievalContext()
        last_detail = "No exact source PDF was found"
        for _round in range(2):
            check_cancelled()
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                return DatasheetRetrievalResult(
                    product=product,
                    document=None,
                    citation=None,
                    detail="Timed out",
                )
            response = self._run_response_with_controls(
                history,
                "broader",
                retrieval_context,
                reasoning_mode,
                client,
                check_cancelled,
                deadline,
                min(45, remaining_seconds),
            )
            check_cancelled()
            history.extend(response.output)
            tool_calls = [
                item
                for item in response.output
                if getattr(item, "type", None) == "function_call"
                and getattr(item, "name", None) == "retrieve_source_pdf"
            ]
            if not tool_calls:
                response_text = str(getattr(response, "output_text", "") or "").strip()
                if response_text:
                    last_detail = "Exact PDF was not attached"
                break

            tool_call = tool_calls[0]
            arguments = json.loads(tool_call.arguments)
            tool_output, citations, summary, generated_result = self._execute_tool(
                tool_call.name,
                arguments,
                "broader",
                retrieval_context,
                reasoning_mode,
                cancel_check=check_cancelled,
            )
            if generated_result is not None:
                citation = citations[0] if citations else None
                document = GeneratedChatDocument(
                    filename=generated_result.filename,
                    mime_type=generated_result.mime_type,
                    content_base64=base64.b64encode(
                        generated_result.content_bytes
                    ).decode("ascii"),
                    title=f"{product} datasheet",
                    document_kind="source",
                    source_url=citation.source_url if citation is not None else None,
                )
                return DatasheetRetrievalResult(
                    product=product,
                    document=document,
                    citation=citation,
                    detail="Retrieved",
                )
            last_detail = str(tool_output.get("message") or summary or last_detail)
            history.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_call.call_id,
                    "output": json.dumps(tool_output, ensure_ascii=True),
                }
            )
            break

        return DatasheetRetrievalResult(
            product=product,
            document=None,
            citation=None,
            detail=last_detail[:160],
        )

    def _finish_limited_response(
        self,
        session: SessionState,
        citations: "OrderedDict[str, Citation]",
        traces: list[ToolTrace],
        generated_document: GeneratedChatDocument | None,
        generated_documents: list[GeneratedChatDocument],
        source_mode: SourceMode,
        reasoning_mode: ReasoningMode,
        context_filter: ContextFilter,
        assistant_message: str,
    ) -> ChatResponse:
        final_citations = list(citations.values())
        session.transcript.append(
            ConversationMessage(
                role="assistant",
                label="Assistant",
                body=assistant_message,
                citations=final_citations,
                tool_trace=traces,
                generated_document=generated_document,
                generated_documents=generated_documents,
            )
        )
        chat_logger.warning(
            "Chat guardrail stopped response: conversation_id=%s message=%s",
            session.conversation_id,
            assistant_message,
        )
        return ChatResponse(
            conversation_id=session.conversation_id,
            assistant_message=assistant_message,
            citations=final_citations,
            tool_trace=traces,
            generated_document=generated_document,
            generated_documents=generated_documents,
            source_mode=source_mode,
            reasoning_mode=reasoning_mode,
            context_filter=context_filter,
        )

    def _run_response(
        self,
        history: list[Any],
        source_mode: SourceMode,
        retrieval_context: RetrievalContext,
        reasoning_mode: ReasoningMode = "standard",
        timeout_seconds: float | None = None,
        client: OpenAI | None = None,
    ) -> Any:
        reasoning_profile = get_chat_reasoning_profile(self._settings, reasoning_mode)
        request: dict[str, Any] = {
            "model": reasoning_profile["model"],
            "input": history,
            "instructions": (
                f"{build_system_prompt(source_mode)}\n\n"
                f"{build_context_scope_prompt(sorted(retrieval_context.folder_ids), sorted(retrieval_context.document_ids), source_mode)}\n\n"
                "If the user asks you to create a downloadable file or business deliverable, call "
                "`generate_context_document` instead of only describing what the file would contain. "
                "If the user asks for an original datasheet, manual, specification, or source PDF, "
                "search the web for the official publisher or manufacturer, prefer its direct PDF URL, "
                "and call `retrieve_source_pdf`. Do not use retrieval to add the file to the internal library."
            ),
            "tools": [
                *([*TOOLS] if source_mode == "internal" or retrieval_context.is_active else []),
                *(
                    [WEB_SEARCH_TOOL, SOURCE_RETRIEVAL_TOOL]
                    if source_mode == "broader"
                    else []
                ),
            ],
            "reasoning": {"effort": reasoning_profile["effort"]},
            "text": {"verbosity": self._settings.openai_text_verbosity},
            "store": self._settings.openai_store_responses,
        }
        if not self._settings.openai_store_responses:
            request["include"] = ["reasoning.encrypted_content"]
        if timeout_seconds is not None:
            request["timeout"] = max(0.1, timeout_seconds)
        return (client or self.client).responses.create(**request)

    def _run_response_with_controls(
        self,
        history: list[Any],
        source_mode: SourceMode,
        retrieval_context: RetrievalContext,
        reasoning_mode: ReasoningMode,
        client: OpenAI,
        cancel_check: Any,
        deadline: float,
        timeout_seconds: float,
    ) -> Any:
        future = _CHAT_MODEL_EXECUTOR.submit(
            self._run_response,
            history,
            source_mode,
            retrieval_context,
            reasoning_mode,
            timeout_seconds,
            client,
        )
        while True:
            cancel_check()
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                try:
                    client.close()
                except Exception:
                    chat_logger.exception("Could not interrupt timed-out model request.")
                future.cancel()
                raise ChatDeadlineError("The chat response exceeded its total time limit.")
            try:
                return future.result(timeout=min(0.1, remaining_seconds))
            except FutureTimeout:
                continue

    def _collect_web_search_metadata(
        self,
        output_items: list[Any],
    ) -> tuple[list[Citation], list[ToolTrace]]:
        citations_by_url: "OrderedDict[str, Citation]" = OrderedDict()
        traces: list[ToolTrace] = []

        for item in output_items:
            item_type = self._output_value(item, "type")
            if item_type == "web_search_call":
                action = self._output_value(item, "action")
                arguments = {
                    key: value
                    for key in ("type", "query", "queries", "url")
                    if (value := self._output_value(action, key)) is not None
                }
                action_type = str(arguments.get("type") or "search").replace("_", " ")
                traces.append(
                    ToolTrace(
                        tool_name="web_search",
                        arguments=arguments,
                        summary=f"Completed web {action_type}.",
                    )
                )

            if item_type != "message":
                continue

            for content_item in self._output_value(item, "content", []) or []:
                for annotation in self._output_value(content_item, "annotations", []) or []:
                    if self._output_value(annotation, "type") != "url_citation":
                        continue
                    url = str(self._output_value(annotation, "url") or "").strip()
                    if not url or url in citations_by_url:
                        continue
                    title = str(self._output_value(annotation, "title") or url).strip() or url
                    citation_id = f"WEB-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12].upper()}"
                    citations_by_url[url] = Citation(
                        document_id=citation_id,
                        title=title,
                        category="web",
                        source_url=url,
                    )

        return list(citations_by_url.values()), traces

    def _output_value(self, item: Any, name: str, default: Any = None) -> Any:
        if isinstance(item, dict):
            return item.get(name, default)
        return getattr(item, name, default)

    def _execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        source_mode: SourceMode,
        retrieval_context: RetrievalContext,
        reasoning_mode: ReasoningMode = "standard",
        cancel_check: Any | None = None,
    ) -> tuple[dict[str, Any], list[Citation], str, GeneratedDocumentResult | None]:
        if tool_name == "search_documents":
            hits = self._document_store.search_documents(
                query=arguments["query"],
                limit=arguments["limit"],
                context=retrieval_context,
                search_profile="answer",
            )
            citations = [
                Citation(
                    document_id=hit.document_id,
                    title=hit.title,
                    category=hit.category,
                    excerpt=redact_sensitive_text(hit.excerpt),
                    source_url=hit.source_url,
                )
                for hit in hits
            ]
            payload = {
                "query": arguments["query"],
                "results": [self._redact_search_hit_payload(hit.to_payload()) for hit in hits],
            }
            summary = f"Found {len(hits)} document hit(s)."
            return payload, citations, summary, None

        if tool_name == "get_document":
            document = self._document_store.get_document(
                arguments["document_id"],
                context=retrieval_context,
            )
            if document is None:
                payload = {
                    "document_id": arguments["document_id"],
                    "found": False,
                    "message": "Document not found or outside the active context scope.",
                }
                return payload, [], "Document lookup returned no result.", None

            citation = Citation(
                document_id=document.document_id,
                title=document.title,
                category=document.category,
                excerpt=redact_sensitive_text(document.summary),
                source_url=document.source_url,
            )
            payload = self._redact_document_payload(document.to_tool_payload())
            return payload, [citation], f"Loaded document {document.document_id}.", None

        if tool_name == "generate_context_document":
            if self._document_generator is None:
                raise RuntimeError("Document generation is not configured.")
            context_filter = ContextFilter(
                folder_ids=sorted(retrieval_context.folder_ids),
                document_ids=sorted(retrieval_context.document_ids),
            )
            generated_result = self._document_generator.generate_document(
                instructions=arguments["instructions"],
                title=arguments.get("title"),
                output_format=arguments["output_format"],
                source_mode=source_mode,
                reasoning_mode=reasoning_mode,
                context_filter=context_filter,
            )
            payload = {
                "filename": generated_result.filename,
                "mime_type": generated_result.mime_type,
                "message": generated_result.message,
                "citations": [citation.model_dump(mode="json") for citation in generated_result.citations],
            }
            summary = f"Generated {generated_result.filename}."
            return payload, generated_result.citations, summary, generated_result

        if tool_name == "retrieve_source_pdf":
            if source_mode != "broader":
                raise ValueError("Internet source retrieval requires Context: Global.")
            try:
                retrieved = self._source_retriever.retrieve_pdf(
                    arguments["url"],
                    filename=arguments.get("filename"),
                    cancel_check=cancel_check,
                )
            except (ValueError, httpx.HTTPError) as exc:
                message = f"Could not retrieve the source PDF: {exc}"
                return (
                    {
                        "retrieved": False,
                        "url": arguments["url"],
                        "message": message,
                    },
                    [],
                    message,
                    None,
                )
            citation = Citation(
                document_id=(
                    "WEB-"
                    f"{hashlib.sha256(retrieved.source_url.encode('utf-8')).hexdigest()[:12].upper()}"
                ),
                title=arguments.get("title") or retrieved.filename,
                category="web",
                source_url=retrieved.source_url,
            )
            result = GeneratedDocumentResult(
                filename=retrieved.filename,
                mime_type=retrieved.mime_type,
                content_bytes=retrieved.content_bytes,
                message="Retrieved the original source PDF without adding it to the library.",
                citations=[citation],
            )
            return (
                {
                    "retrieved": True,
                    "filename": result.filename,
                    "mime_type": result.mime_type,
                    "source_url": retrieved.source_url,
                    "message": result.message,
                },
                [citation],
                f"Retrieved original source PDF {result.filename}.",
                result,
            )

        raise ValueError(f"Unknown tool: {tool_name}")

    def _redact_search_hit_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(payload)
        for key in ("summary", "excerpt"):
            redacted[key] = redact_sensitive_text(redacted.get(key))
        return redacted

    def _redact_document_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(payload)
        for key in ("summary", "text"):
            redacted[key] = redact_sensitive_text(redacted.get(key))
        return redacted

    def _select_final_citations(
        self,
        assistant_message: str,
        citations: "OrderedDict[str, Citation]",
        traces: list[ToolTrace],
    ) -> list[Citation]:
        web_citations = [
            citation
            for citation in citations.values()
            if citation.category == "web" and citation.source_url
        ]
        internal_citations = OrderedDict(
            (document_id, citation)
            for document_id, citation in citations.items()
            if citation.category != "web"
        )
        explicit_ids = list(
            OrderedDict.fromkeys(
                match.group(1)
                for match in INLINE_CITATION_RE.finditer(assistant_message)
            )
        )
        if explicit_ids:
            selected_internal = [
                internal_citations[document_id]
                for document_id in explicit_ids
                if document_id in internal_citations
            ]
            return [*selected_internal, *web_citations]

        loaded_ids = list(
            OrderedDict.fromkeys(
                trace.arguments["document_id"]
                for trace in traces
                if trace.tool_name == "get_document" and trace.arguments.get("document_id") in internal_citations
            )
        )
        if loaded_ids:
            return [
                *(internal_citations[document_id] for document_id in loaded_ids),
                *web_citations,
            ]

        return [*internal_citations.values(), *web_citations]
