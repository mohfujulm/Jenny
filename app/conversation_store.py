from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading

from app.models import ConversationMessage, SavedConversationDetail, SavedConversationSummary, SessionState


def _copy_message(message: ConversationMessage) -> ConversationMessage:
    return ConversationMessage.model_validate(message.model_dump())


def _copy_conversation(conversation: SavedConversationDetail) -> SavedConversationDetail:
    return SavedConversationDetail.model_validate(conversation.model_dump())


class SavedConversationStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def list_conversations(self) -> list[SavedConversationSummary]:
        with self._lock:
            conversations = self._load_conversations_locked()
        return [self._to_summary(item) for item in conversations]

    def get_conversation(self, conversation_id: str) -> SavedConversationDetail | None:
        with self._lock:
            conversations = self._load_conversations_locked()
            conversation = next(
                (item for item in conversations if item.conversation_id == conversation_id),
                None,
            )
        return None if conversation is None else _copy_conversation(conversation)

    def save_session(self, session: SessionState, title: str | None = None) -> SavedConversationDetail:
        if not session.transcript:
            raise ValueError("Cannot save an empty conversation.")

        with self._lock:
            conversations = self._load_conversations_locked()
            existing = next(
                (item for item in conversations if item.conversation_id == session.conversation_id),
                None,
            )
            normalized_title = self._normalize_explicit_title(title)
            if normalized_title is not None:
                resolved_title = normalized_title
                title_is_custom = True
            elif existing is not None and existing.title_is_custom and existing.title:
                resolved_title = existing.title
                title_is_custom = True
            else:
                resolved_title = None
                title_is_custom = False
            conversation = SavedConversationDetail(
                conversation_id=session.conversation_id,
                title=resolved_title,
                title_is_custom=title_is_custom,
                summary=self._resolve_summary(session.transcript),
                created_at=existing.created_at if existing is not None else session.created_at.isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
                message_count=len(session.transcript),
                source_mode=session.source_mode,
                context_filter=session.context_filter.model_copy(deep=True),
                messages=[_copy_message(message) for message in session.transcript],
            )
            next_conversations = [
                item for item in conversations if item.conversation_id != session.conversation_id
            ]
            next_conversations.append(conversation)
            next_conversations.sort(key=lambda item: item.updated_at, reverse=True)
            self._write_conversations_locked(next_conversations)
        return _copy_conversation(conversation)

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._lock:
            conversations = self._load_conversations_locked()
            next_conversations = [
                item for item in conversations if item.conversation_id != conversation_id
            ]
            deleted = len(next_conversations) != len(conversations)
            if deleted:
                self._write_conversations_locked(next_conversations)
        return deleted

    def _load_conversations_locked(self) -> list[SavedConversationDetail]:
        if not self._path.exists():
            return []

        payload = json.loads(self._path.read_text(encoding="utf-8"))
        items = payload.get("conversations", []) if isinstance(payload, dict) else []
        conversations: list[SavedConversationDetail] = []
        for item in items:
            conversation = SavedConversationDetail.model_validate(item)
            conversation.summary = self._resolve_summary(conversation.messages)
            conversations.append(conversation)
        conversations.sort(key=lambda item: item.updated_at, reverse=True)
        return conversations

    def _write_conversations_locked(self, conversations: list[SavedConversationDetail]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "conversations": [
                conversation.model_dump(mode="json")
                for conversation in conversations
            ]
        }
        self._path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _normalize_explicit_title(self, explicit_title: str | None) -> str | None:
        normalized_title = " ".join((explicit_title or "").split())
        if not normalized_title:
            return None
        return normalized_title[:120]

    def _resolve_summary(self, transcript: list[ConversationMessage]) -> str:
        for message in transcript:
            if message.role != "user":
                continue
            normalized = self._normalize_summary_seed(message.body)
            if normalized:
                return normalized

        for message in transcript:
            normalized = self._normalize_summary_seed(message.body)
            if normalized:
                return normalized

        return "Saved conversation"

    def _normalize_summary_seed(self, value: str) -> str:
        normalized = " ".join((value or "").replace("\r", " ").replace("\n", " ").split())
        if not normalized:
            return ""

        summary = normalized.strip().strip("`").strip()
        issue_hint = bool(re.search(r"\b(issue|problem|error|outage|failure)\b", summary, flags=re.IGNORECASE))
        summary = summary.split("```", 1)[0].strip()
        summary = summary.split(":", 1)[-1].strip() if summary.lower().startswith("summary:") else summary
        summary = re.sub(r"[?!.]+$", "", summary).strip()
        summary = re.sub(
            r"^(?:what(?:'s| is)\s+the\s+(?:issue|problem|error)\s+(?:on|with|in)\s+(?:the\s+)?)",
            "",
            summary,
            flags=re.IGNORECASE,
        ).strip()

        leading_patterns = [
            r"^(?:can|could|would|will)\s+you\s+",
            r"^please\s+",
            r"^how\s+do\s+i\s+",
            r"^how\s+can\s+i\s+",
            r"^how\s+do\s+we\s+",
            r"^how\s+can\s+we\s+",
            r"^what\s+is\s+",
            r"^what\s+are\s+",
            r"^what'?s\s+",
            r"^tell\s+me\s+about\s+",
            r"^help\s+me\s+(?:with\s+)?",
            r"^i\s+need\s+to\s+",
            r"^i\s+want\s+to\s+",
            r"^we\s+need\s+to\s+",
            r"^we\s+want\s+to\s+",
            r"^show\s+me\s+",
            r"^explain\s+",
            r"^draft\s+",
            r"^create\s+",
            r"^generate\s+",
            r"^let'?s\s+",
        ]
        for pattern in leading_patterns:
            summary = re.sub(pattern, "", summary, flags=re.IGNORECASE).strip()

        trailing_patterns = [
            r"\s+(?:and|but)\s+(?:how|what|why|when|where|can|could|would|should|do|does|is|are)\b.*$",
            r"\s+\bif\b.*$",
            r"\s+\bbecause\b.*$",
            r"\s+\bso that\b.*$",
        ]
        for pattern in trailing_patterns:
            summary = re.sub(pattern, "", summary, flags=re.IGNORECASE).strip()

        summary = re.sub(r"^(?:the|a|an)\s+", "", summary, flags=re.IGNORECASE).strip()
        summary = re.sub(r"\s+", " ", summary).strip(" -_.,:;!?")
        if not summary:
            return ""

        tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9'/-]*", summary)
        if not tokens:
            return ""

        if tokens and tokens[0].lower() in {
            "maintain",
            "update",
            "create",
            "generate",
            "draft",
            "explain",
            "fix",
            "resolve",
            "rename",
            "delete",
            "move",
            "upload",
            "save",
            "open",
            "use",
            "find",
            "show",
            "review",
            "check",
        }:
            tokens = tokens[1:]

        compact_tokens: list[str] = []
        removable_words = {
            "the",
            "a",
            "an",
            "my",
            "our",
            "your",
            "their",
            "through",
            "with",
            "for",
            "into",
            "from",
            "that",
            "this",
            "these",
            "those",
            "part",
            "one",
            "it",
        }
        for token in tokens:
            if len(compact_tokens) >= 6:
                break
            if compact_tokens and token.lower() in removable_words:
                continue
            compact_tokens.append(token)

        if not compact_tokens:
            return ""

        if issue_hint and compact_tokens[-1].lower() not in {"issue", "problem", "error", "outage", "failure"}:
            if len(compact_tokens) >= 6:
                compact_tokens = compact_tokens[:5]
            compact_tokens.append("issue")

        summary = " ".join(compact_tokens).strip()
        if not summary:
            return ""
        summary = summary[0].upper() + summary[1:]
        return self._truncate_title(summary)

    def _to_summary(self, conversation: SavedConversationDetail) -> SavedConversationSummary:
        return SavedConversationSummary(
            conversation_id=conversation.conversation_id,
            title=conversation.title,
            title_is_custom=conversation.title_is_custom,
            summary=conversation.summary,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            message_count=conversation.message_count,
            source_mode=conversation.source_mode,
        )

    def _truncate_title(self, value: str) -> str:
        max_length = 72
        if len(value) <= max_length:
            return value
        return f"{value[: max_length - 3].rstrip()}..."
