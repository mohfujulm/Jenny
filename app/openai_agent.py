from __future__ import annotations

import base64
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import json
import re
import threading
import uuid
from typing import Any

from openai import OpenAI

from app.config import Settings
from app.conversation_store import SavedConversationStore
from app.datastore import BaseDocumentStore, RetrievalContext
from app.document_generator import ContextDocumentGenerator, GeneratedDocumentResult
from app.models import (
    ChatResponse,
    Citation,
    ContextFilter,
    ConversationMessage,
    GeneratedChatDocument,
    SavedConversationDetail,
    SavedConversationSummary,
    SessionState,
    SourceMode,
    ToolTrace,
)
from app.prompts import build_context_scope_prompt, build_system_prompt
from app.sensitive_text import redact_sensitive_text


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
            "yourself: use docx for formal documents, xlsx for spreadsheets/tables/trackers, and txt for plain text."
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
                    "enum": ["txt", "docx", "xlsx"],
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

INLINE_CITATION_RE = re.compile(r"\[([A-Za-z0-9][A-Za-z0-9_-]*)\]")


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

    def get_or_create(self, conversation_id: str | None) -> SessionState:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._prune_locked(now)
            session_id = conversation_id or str(uuid.uuid4())
            session = self._sessions.get(session_id)
            if session is None:
                session = self._load_saved_session_locked(session_id, now)
                if session is None:
                    session = SessionState(conversation_id=session_id)
                self._sessions[session_id] = session
            session.last_touched = now
            return session

    def list_saved_conversations(self) -> list[SavedConversationSummary]:
        if self._saved_conversations is None:
            return []
        return self._saved_conversations.list_conversations()

    def load_saved_conversation(self, conversation_id: str) -> SavedConversationDetail | None:
        if self._saved_conversations is None:
            return None
        conversation = self._saved_conversations.get_conversation(conversation_id)
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
        title: str | None = None,
    ) -> SavedConversationDetail:
        if self._saved_conversations is None:
            raise RuntimeError("Saved conversation storage is not configured.")
        session = self.get_or_create(conversation_id)
        return self._saved_conversations.save_session(session, title=title)

    def delete_saved_conversation(self, conversation_id: str) -> bool:
        if self._saved_conversations is None:
            raise RuntimeError("Saved conversation storage is not configured.")
        return self._saved_conversations.delete_conversation(conversation_id)

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
        now: datetime,
    ) -> SessionState | None:
        if self._saved_conversations is None:
            return None
        conversation = self._saved_conversations.get_conversation(conversation_id)
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
            history=self._build_history_from_messages(conversation.messages),
            transcript=[
                ConversationMessage.model_validate(message.model_dump())
                for message in conversation.messages
            ],
            source_mode=conversation.source_mode,
            context_filter=conversation.context_filter.model_copy(deep=True),
            created_at=datetime.fromisoformat(conversation.created_at),
            last_touched=now,
        )

    def _build_history_from_messages(self, messages: list[ConversationMessage]) -> list[Any]:
        history: list[Any] = []
        for message in messages:
            if message.role not in {"user", "assistant"}:
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
        self._client: OpenAI | None = None

    @property
    def client(self) -> OpenAI:
        if not self._settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured.")
        if self._client is None:
            self._client = OpenAI(api_key=self._settings.openai_api_key)
        return self._client

    def chat(
        self,
        conversation_id: str | None,
        message: str,
        source_mode: SourceMode,
        context_filter: ContextFilter,
    ) -> ChatResponse:
        session = self._sessions.get_or_create(conversation_id)
        session.source_mode = source_mode
        session.context_filter = context_filter.model_copy(deep=True)
        session.history.append({"role": "user", "content": message})
        session.transcript.append(
            ConversationMessage(
                role="user",
                label="You",
                body=message,
            )
        )
        retrieval_context = RetrievalContext.from_lists(
            folder_ids=context_filter.folder_ids,
            document_ids=context_filter.document_ids,
        )

        citations: "OrderedDict[str, Citation]" = OrderedDict()
        traces: list[ToolTrace] = []
        generated_document: GeneratedChatDocument | None = None

        while True:
            response = self._run_response(session.history, source_mode, retrieval_context)
            session.history.extend(response.output)

            tool_calls = [item for item in response.output if getattr(item, "type", None) == "function_call"]
            if not tool_calls:
                assistant_message = response.output_text.strip()
                if not assistant_message:
                    assistant_message = "I could not produce a final answer."
                final_citations = self._select_final_citations(
                    assistant_message=assistant_message,
                    citations=citations,
                    traces=traces,
                )
                session.transcript.append(
                    ConversationMessage(
                        role="assistant",
                        label="Assistant",
                        body=assistant_message,
                        citations=final_citations,
                        tool_trace=traces,
                        generated_document=generated_document,
                    )
                )
                return ChatResponse(
                    conversation_id=session.conversation_id,
                    assistant_message=assistant_message,
                    citations=final_citations,
                    tool_trace=traces,
                    generated_document=generated_document,
                    source_mode=source_mode,
                    context_filter=context_filter,
                )

            for tool_call in tool_calls:
                arguments = json.loads(tool_call.arguments)
                tool_output, new_citations, summary, generated_result = self._execute_tool(
                    tool_call.name,
                    arguments,
                    source_mode,
                    retrieval_context,
                )
                for citation in new_citations:
                    citations[citation.document_id] = citation
                if generated_result is not None:
                    generated_document = GeneratedChatDocument(
                        filename=generated_result.filename,
                        mime_type=generated_result.mime_type,
                        content_base64=base64.b64encode(generated_result.content_bytes).decode("ascii"),
                        title=arguments.get("title"),
                    )
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

    def _run_response(
        self,
        history: list[Any],
        source_mode: SourceMode,
        retrieval_context: RetrievalContext,
    ) -> Any:
        request: dict[str, Any] = {
            "model": self._settings.openai_model,
            "input": history,
            "instructions": (
                f"{build_system_prompt(source_mode)}\n\n"
                f"{build_context_scope_prompt(sorted(retrieval_context.folder_ids), sorted(retrieval_context.document_ids))}\n\n"
                "If the user asks you to create a downloadable file or business deliverable, call "
                "`generate_context_document` instead of only describing what the file would contain."
            ),
            "tools": TOOLS,
            "reasoning": {"effort": self._settings.openai_reasoning_effort},
            "text": {"verbosity": self._settings.openai_text_verbosity},
            "store": self._settings.openai_store_responses,
        }
        if not self._settings.openai_store_responses:
            request["include"] = ["reasoning.encrypted_content"]
        return self.client.responses.create(**request)

    def _execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        source_mode: SourceMode,
        retrieval_context: RetrievalContext,
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
        explicit_ids = list(
            OrderedDict.fromkeys(
                match.group(1)
                for match in INLINE_CITATION_RE.finditer(assistant_message)
            )
        )
        if explicit_ids:
            return [citations[document_id] for document_id in explicit_ids if document_id in citations]

        loaded_ids = list(
            OrderedDict.fromkeys(
                trace.arguments["document_id"]
                for trace in traces
                if trace.tool_name == "get_document" and trace.arguments.get("document_id") in citations
            )
        )
        if loaded_ids:
            return [citations[document_id] for document_id in loaded_ids]

        return list(citations.values())
