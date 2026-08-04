"""Transactional, normalized persistence for private saved conversations.

Conversation metadata, messages, and binary attachments live in SQLite rather
than one global JSON document. SQLite transactions and a write-ahead log make
saves atomic across process interruption, while normalized BLOB references
avoid storing base64 data (or the legacy/current generated-document fields)
twice. A legacy JSON file is imported once and then left untouched as a
recovery copy.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading

from app.models import (
    ChatImage,
    Citation,
    ContextFilter,
    ConversationMessage,
    GeneratedChatDocument,
    SavedConversationDetail,
    SavedConversationSummary,
    SessionState,
    ToolTrace,
)


_SCHEMA_VERSION = "2"
_LEGACY_MIGRATION_KEY = "legacy_json_migration_v1"


class SavedConversationStore:
    """Persist conversations atomically and enforce owner isolation in SQL."""

    def __init__(
        self,
        path: Path,
        default_owner_user_id: str,
        *,
        legacy_json_path: Path | None = None,
    ) -> None:
        supplied_path = Path(path)
        if supplied_path.suffix.lower() == ".json":
            self._database_path = supplied_path.with_suffix(".sqlite")
            self._legacy_json_path = Path(legacy_json_path or supplied_path)
        else:
            self._database_path = supplied_path
            self._legacy_json_path = (
                Path(legacy_json_path) if legacy_json_path is not None else None
            )
        self._default_owner_user_id = str(default_owner_user_id or "").strip()
        if not self._default_owner_user_id:
            raise ValueError("A default conversation owner is required.")
        self._lock = threading.RLock()
        self._initialize()
        self._migrate_legacy_json_once()

    @property
    def database_path(self) -> Path:
        """Return the SQLite path for diagnostics, backups, and tests."""
        return self._database_path

    @property
    def legacy_json_path(self) -> Path | None:
        """Return the optional read-only migration source path."""
        return self._legacy_json_path

    def list_conversations(self, owner_user_id: str) -> list[SavedConversationSummary]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT c.*, COUNT(m.message_index) AS message_count
                FROM conversations AS c
                LEFT JOIN conversation_messages AS m
                  ON m.conversation_id = c.conversation_id
                WHERE c.owner_user_id = ?
                GROUP BY c.conversation_id
                ORDER BY c.updated_at DESC
                """,
                (owner_user_id,),
            ).fetchall()
        return [self._summary_from_row(row) for row in rows]

    def get_conversation(
        self,
        conversation_id: str,
        owner_user_id: str,
    ) -> SavedConversationDetail | None:
        with self._lock, closing(self._connect()) as connection:
            conversation = self._load_conversation_locked(
                connection,
                conversation_id,
                owner_user_id=owner_user_id,
            )
        # Every read is freshly materialized from SQLite; callers cannot mutate
        # any cached or shared storage object.
        return conversation

    def get_conversation_owner(self, conversation_id: str) -> str | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT owner_user_id FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return None if row is None else str(row["owner_user_id"])

    def save_session(
        self,
        session: SessionState,
        title: str | None = None,
    ) -> SavedConversationDetail:
        if not session.transcript:
            raise ValueError("Cannot save an empty conversation.")

        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT owner_user_id, title, title_is_custom, created_at
                    FROM conversations
                    WHERE conversation_id = ?
                    """,
                    (session.conversation_id,),
                ).fetchone()
                if (
                    existing is not None
                    and str(existing["owner_user_id"]) != session.owner_user_id
                ):
                    raise PermissionError("Conversation belongs to another user.")

                normalized_title = self._normalize_explicit_title(title)
                if normalized_title is not None:
                    resolved_title = normalized_title
                    title_is_custom = True
                elif (
                    existing is not None
                    and bool(existing["title_is_custom"])
                    and existing["title"]
                ):
                    resolved_title = str(existing["title"])
                    title_is_custom = True
                else:
                    resolved_title = None
                    title_is_custom = False

                conversation = SavedConversationDetail(
                    conversation_id=session.conversation_id,
                    owner_user_id=session.owner_user_id,
                    title=resolved_title,
                    title_is_custom=title_is_custom,
                    summary=self._resolve_summary(session.transcript),
                    created_at=(
                        str(existing["created_at"])
                        if existing is not None
                        else session.created_at.isoformat()
                    ),
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    message_count=len(session.transcript),
                    source_mode=session.source_mode,
                    reasoning_mode=session.reasoning_mode,
                    context_filter=session.context_filter.model_copy(deep=True),
                    messages=session.transcript,
                )
                self._write_conversation_locked(connection, conversation)
                self._delete_orphaned_blobs_locked(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

            saved = self._load_conversation_locked(
                connection,
                session.conversation_id,
                owner_user_id=session.owner_user_id,
            )
        if saved is None:
            raise RuntimeError("The conversation was saved but could not be reloaded.")
        return saved

    def delete_conversation(self, conversation_id: str, owner_user_id: str) -> bool:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    DELETE FROM conversations
                    WHERE conversation_id = ? AND owner_user_id = ?
                    """,
                    (conversation_id, owner_user_id),
                )
                deleted = cursor.rowcount > 0
                if deleted:
                    self._delete_orphaned_blobs_locked(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return deleted

    def delete_message_pair(
        self,
        conversation_id: str,
        assistant_message_index: int,
        owner_user_id: str,
    ) -> SavedConversationDetail | None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                conversation = self._load_conversation_locked(
                    connection,
                    conversation_id,
                    owner_user_id=owner_user_id,
                )
                if conversation is None:
                    connection.rollback()
                    return None

                messages = conversation.messages
                if assistant_message_index < 1 or assistant_message_index >= len(messages):
                    raise ValueError("Selected response pair is not valid.")
                if (
                    messages[assistant_message_index].role != "assistant"
                    or messages[assistant_message_index - 1].role != "user"
                ):
                    raise ValueError(
                        "Selected message is not a complete question/response pair."
                    )

                conversation.messages = [
                    message
                    for index, message in enumerate(messages)
                    if index not in {assistant_message_index - 1, assistant_message_index}
                ]
                conversation.message_count = len(conversation.messages)
                conversation.summary = self._resolve_summary(conversation.messages)
                conversation.updated_at = datetime.now(timezone.utc).isoformat()
                self._write_conversation_locked(connection, conversation)
                self._delete_orphaned_blobs_locked(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

            saved = self._load_conversation_locked(
                connection,
                conversation_id,
                owner_user_id=owner_user_id,
            )
        return saved

    def _initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    title TEXT,
                    title_is_custom INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source_mode TEXT NOT NULL,
                    reasoning_mode TEXT NOT NULL,
                    context_filter_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_conversations_owner_updated
                ON conversations(owner_user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS conversation_messages (
                    conversation_id TEXT NOT NULL,
                    message_index INTEGER NOT NULL,
                    message_fingerprint TEXT NOT NULL,
                    role TEXT NOT NULL,
                    label TEXT NOT NULL,
                    body TEXT NOT NULL,
                    citations_json TEXT NOT NULL,
                    tool_trace_json TEXT NOT NULL,
                    PRIMARY KEY (conversation_id, message_index),
                    FOREIGN KEY (conversation_id)
                        REFERENCES conversations(conversation_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS conversation_blobs (
                    blob_id TEXT PRIMARY KEY,
                    content BLOB NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_message_images (
                    conversation_id TEXT NOT NULL,
                    message_index INTEGER NOT NULL,
                    image_index INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    blob_id TEXT NOT NULL,
                    PRIMARY KEY (conversation_id, message_index, image_index),
                    FOREIGN KEY (conversation_id, message_index)
                        REFERENCES conversation_messages(conversation_id, message_index)
                        ON DELETE CASCADE,
                    FOREIGN KEY (blob_id)
                        REFERENCES conversation_blobs(blob_id)
                );

                CREATE INDEX IF NOT EXISTS idx_conversation_images_blob
                ON conversation_message_images(blob_id);

                CREATE TABLE IF NOT EXISTS conversation_message_documents (
                    conversation_id TEXT NOT NULL,
                    message_index INTEGER NOT NULL,
                    document_index INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    title TEXT,
                    document_kind TEXT NOT NULL,
                    source_url TEXT,
                    blob_id TEXT NOT NULL,
                    PRIMARY KEY (conversation_id, message_index, document_index),
                    FOREIGN KEY (conversation_id, message_index)
                        REFERENCES conversation_messages(conversation_id, message_index)
                        ON DELETE CASCADE,
                    FOREIGN KEY (blob_id)
                        REFERENCES conversation_blobs(blob_id)
                );

                CREATE INDEX IF NOT EXISTS idx_conversation_documents_blob
                ON conversation_message_documents(blob_id);
                """
            )
            message_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(conversation_messages)"
                ).fetchall()
            }
            if "message_fingerprint" not in message_columns:
                connection.execute(
                    """
                    ALTER TABLE conversation_messages
                    ADD COLUMN message_fingerprint TEXT NOT NULL DEFAULT ''
                    """
                )
            connection.execute(
                """
                INSERT INTO conversation_store_meta(key, value)
                VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (_SCHEMA_VERSION,),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _migrate_legacy_json_once(self) -> None:
        legacy_path = self._legacy_json_path
        if legacy_path is None or not legacy_path.exists():
            return

        # Hashing the read-only source lets a transition from an already-running
        # JSON-based process reconcile one final newer write on the next restart.
        legacy_bytes = legacy_path.read_bytes()
        legacy_hash = hashlib.sha256(legacy_bytes).hexdigest()
        payload = json.loads(legacy_bytes.decode("utf-8"))
        raw_items = (
            payload.get("conversations", [])
            if isinstance(payload, dict)
            else []
        )

        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                migration = connection.execute(
                    "SELECT value FROM conversation_store_meta WHERE key = ?",
                    (_LEGACY_MIGRATION_KEY,),
                ).fetchone()
                previous_migration: dict[str, object] = {}
                if migration is not None:
                    try:
                        parsed_migration = json.loads(str(migration["value"]))
                        if isinstance(parsed_migration, dict):
                            previous_migration = parsed_migration
                    except json.JSONDecodeError:
                        previous_migration = {}
                    if previous_migration.get("disabled") is True:
                        connection.rollback()
                        return
                    if previous_migration.get("source_sha256") == legacy_hash:
                        connection.rollback()
                        return

                existing_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM conversations"
                    ).fetchone()[0]
                )
                if existing_count and migration is None:
                    self._set_meta_locked(
                        connection,
                        _LEGACY_MIGRATION_KEY,
                        json.dumps(
                            {
                                "disabled": True,
                                "reason": "skipped_nonempty_database",
                                "source": str(legacy_path.resolve()),
                                "source_sha256": legacy_hash,
                            },
                            ensure_ascii=True,
                        ),
                    )
                    connection.commit()
                    return

                previous_ids = {
                    str(item)
                    for item in previous_migration.get("conversation_ids", [])
                    if str(item).strip()
                }
                previous_completed_at = str(
                    previous_migration.get("completed_at") or ""
                )
                legacy_ids: set[str] = set()
                imported_count = 0
                for raw_item in raw_items:
                    if not isinstance(raw_item, dict):
                        continue
                    normalized_item = dict(raw_item)
                    if not normalized_item.get("owner_user_id"):
                        normalized_item["owner_user_id"] = self._default_owner_user_id
                    conversation = SavedConversationDetail.model_validate(
                        normalized_item
                    )
                    conversation.summary = self._resolve_summary(
                        conversation.messages
                    )
                    conversation.message_count = len(conversation.messages)
                    legacy_ids.add(conversation.conversation_id)
                    existing = connection.execute(
                        """
                        SELECT updated_at FROM conversations
                        WHERE conversation_id = ?
                        """,
                        (conversation.conversation_id,),
                    ).fetchone()
                    if (
                        existing is None
                        or self._timestamp_is_newer(
                            conversation.updated_at,
                            str(existing["updated_at"]),
                        )
                    ):
                        self._write_conversation_locked(connection, conversation)
                        imported_count += 1

                removed_count = 0
                if previous_ids and previous_completed_at:
                    for removed_id in previous_ids - legacy_ids:
                        existing = connection.execute(
                            """
                            SELECT updated_at FROM conversations
                            WHERE conversation_id = ?
                            """,
                            (removed_id,),
                        ).fetchone()
                        if existing is None:
                            continue
                        if not self._timestamp_is_newer(
                            str(existing["updated_at"]),
                            previous_completed_at,
                        ):
                            connection.execute(
                                "DELETE FROM conversations WHERE conversation_id = ?",
                                (removed_id,),
                            )
                            removed_count += 1

                completed_at = datetime.now(timezone.utc).isoformat()
                self._set_meta_locked(
                    connection,
                    _LEGACY_MIGRATION_KEY,
                    json.dumps(
                        {
                            "source": str(legacy_path.resolve()),
                            "imported": imported_count,
                            "removed": removed_count,
                            "source_sha256": legacy_hash,
                            "conversation_ids": sorted(legacy_ids),
                            "completed_at": completed_at,
                        },
                        ensure_ascii=True,
                    ),
                )
                self._delete_orphaned_blobs_locked(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _timestamp_is_newer(candidate: str, existing: str) -> bool:
        try:
            return datetime.fromisoformat(candidate) > datetime.fromisoformat(existing)
        except (TypeError, ValueError):
            return candidate > existing

    def _set_meta_locked(
        self,
        connection: sqlite3.Connection,
        key: str,
        value: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO conversation_store_meta(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def _write_conversation_locked(
        self,
        connection: sqlite3.Connection,
        conversation: SavedConversationDetail,
    ) -> None:
        prepared_messages: list[
            tuple[
                ConversationMessage,
                str,
                str,
                list[GeneratedChatDocument],
                str,
            ]
        ] = []
        for message in conversation.messages:
            citations_json = json.dumps(
                [citation.model_dump(mode="json") for citation in message.citations],
                ensure_ascii=True,
                separators=(",", ":"),
            )
            tool_trace_json = json.dumps(
                [trace.model_dump(mode="json") for trace in message.tool_trace],
                ensure_ascii=True,
                separators=(",", ":"),
            )
            documents = self._normalized_generated_documents(message)
            fingerprint = self._message_fingerprint(
                message,
                citations_json=citations_json,
                tool_trace_json=tool_trace_json,
                documents=documents,
            )
            prepared_messages.append(
                (
                    message,
                    citations_json,
                    tool_trace_json,
                    documents,
                    fingerprint,
                )
            )

        connection.execute(
            """
            INSERT INTO conversations (
                conversation_id, owner_user_id, title, title_is_custom,
                summary, created_at, updated_at, source_mode, reasoning_mode,
                context_filter_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                owner_user_id = excluded.owner_user_id,
                title = excluded.title,
                title_is_custom = excluded.title_is_custom,
                summary = excluded.summary,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                source_mode = excluded.source_mode,
                reasoning_mode = excluded.reasoning_mode,
                context_filter_json = excluded.context_filter_json
            """,
            (
                conversation.conversation_id,
                conversation.owner_user_id,
                conversation.title,
                int(conversation.title_is_custom),
                conversation.summary,
                conversation.created_at,
                conversation.updated_at,
                conversation.source_mode,
                conversation.reasoning_mode,
                conversation.context_filter.model_dump_json(),
            ),
        )

        existing_rows = connection.execute(
            """
            SELECT message_index, message_fingerprint
            FROM conversation_messages
            WHERE conversation_id = ?
            ORDER BY message_index
            """,
            (conversation.conversation_id,),
        ).fetchall()
        common_prefix = 0
        for message_index, prepared in enumerate(prepared_messages):
            if message_index >= len(existing_rows):
                break
            existing_row = existing_rows[message_index]
            if (
                int(existing_row["message_index"]) != message_index
                or str(existing_row["message_fingerprint"]) != prepared[4]
            ):
                break
            common_prefix += 1

        if common_prefix < len(existing_rows):
            connection.execute(
                """
                DELETE FROM conversation_messages
                WHERE conversation_id = ? AND message_index >= ?
                """,
                (conversation.conversation_id, common_prefix),
            )

        for message_index in range(common_prefix, len(prepared_messages)):
            (
                message,
                citations_json,
                tool_trace_json,
                documents,
                fingerprint,
            ) = prepared_messages[message_index]
            self._insert_message_locked(
                connection,
                conversation.conversation_id,
                message_index,
                message,
                citations_json=citations_json,
                tool_trace_json=tool_trace_json,
                documents=documents,
                fingerprint=fingerprint,
            )

    def _insert_message_locked(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        message_index: int,
        message: ConversationMessage,
        *,
        citations_json: str,
        tool_trace_json: str,
        documents: list[GeneratedChatDocument],
        fingerprint: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO conversation_messages (
                conversation_id, message_index, message_fingerprint,
                role, label, body, citations_json, tool_trace_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                message_index,
                fingerprint,
                message.role,
                message.label,
                message.body,
                citations_json,
                tool_trace_json,
            ),
        )
        self._write_message_images_locked(
            connection,
            conversation_id,
            message_index,
            message.images,
        )
        self._write_message_documents_locked(
            connection,
            conversation_id,
            message_index,
            documents,
        )

    def _write_message_images_locked(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        message_index: int,
        images: list[ChatImage],
    ) -> None:
        for image_index, image in enumerate(images):
            content = self._decode_base64(
                image.content_base64,
                label=f"image `{image.filename}`",
            )
            blob_id = self._write_blob_locked(connection, content)
            connection.execute(
                """
                INSERT INTO conversation_message_images (
                    conversation_id, message_index, image_index,
                    filename, mime_type, blob_id
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    message_index,
                    image_index,
                    image.filename,
                    image.mime_type,
                    blob_id,
                ),
            )

    def _write_message_documents_locked(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        message_index: int,
        documents: list[GeneratedChatDocument],
    ) -> None:
        for document_index, document in enumerate(documents):
            content = self._decode_base64(
                document.content_base64,
                label=f"generated document `{document.filename}`",
            )
            blob_id = self._write_blob_locked(connection, content)
            connection.execute(
                """
                INSERT INTO conversation_message_documents (
                    conversation_id, message_index, document_index,
                    filename, mime_type, title, document_kind, source_url,
                    blob_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    message_index,
                    document_index,
                    document.filename,
                    document.mime_type,
                    document.title,
                    document.document_kind,
                    document.source_url,
                    blob_id,
                ),
            )

    def _write_blob_locked(
        self,
        connection: sqlite3.Connection,
        content: bytes,
    ) -> str:
        blob_id = hashlib.sha256(content).hexdigest()
        existing = connection.execute(
            "SELECT size_bytes FROM conversation_blobs WHERE blob_id = ?",
            (blob_id,),
        ).fetchone()
        if existing is not None:
            if int(existing["size_bytes"]) != len(content):
                raise RuntimeError("Conversation BLOB hash collision detected.")
            return blob_id
        connection.execute(
            """
            INSERT INTO conversation_blobs(blob_id, content, size_bytes, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                blob_id,
                sqlite3.Binary(content),
                len(content),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return blob_id

    def _load_conversation_locked(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        *,
        owner_user_id: str | None = None,
    ) -> SavedConversationDetail | None:
        parameters: list[str] = [conversation_id]
        owner_clause = ""
        if owner_user_id is not None:
            owner_clause = " AND owner_user_id = ?"
            parameters.append(owner_user_id)
        row = connection.execute(
            f"SELECT * FROM conversations WHERE conversation_id = ?{owner_clause}",
            parameters,
        ).fetchone()
        if row is None:
            return None

        message_rows = connection.execute(
            """
            SELECT * FROM conversation_messages
            WHERE conversation_id = ?
            ORDER BY message_index
            """,
            (conversation_id,),
        ).fetchall()
        images_by_message = self._load_images_locked(connection, conversation_id)
        documents_by_message = self._load_documents_locked(connection, conversation_id)

        messages: list[ConversationMessage] = []
        for message_row in message_rows:
            message_index = int(message_row["message_index"])
            documents = documents_by_message.get(message_index, [])
            messages.append(
                ConversationMessage(
                    role=str(message_row["role"]),
                    label=str(message_row["label"]),
                    body=str(message_row["body"]),
                    images=images_by_message.get(message_index, []),
                    citations=[
                        Citation.model_validate(item)
                        for item in json.loads(str(message_row["citations_json"]))
                    ],
                    tool_trace=[
                        ToolTrace.model_validate(item)
                        for item in json.loads(str(message_row["tool_trace_json"]))
                    ],
                    # The singular field is accepted only while importing old
                    # data. New saved-conversation payloads use the list once.
                    generated_document=None,
                    generated_documents=documents,
                )
            )

        context_filter = ContextFilter.model_validate_json(
            str(row["context_filter_json"])
        )
        return SavedConversationDetail(
            conversation_id=str(row["conversation_id"]),
            owner_user_id=str(row["owner_user_id"]),
            title=None if row["title"] is None else str(row["title"]),
            title_is_custom=bool(row["title_is_custom"]),
            summary=str(row["summary"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            message_count=len(messages),
            source_mode=str(row["source_mode"]),
            reasoning_mode=str(row["reasoning_mode"]),
            context_filter=context_filter,
            messages=messages,
        )

    def _load_images_locked(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
    ) -> dict[int, list[ChatImage]]:
        rows = connection.execute(
            """
            SELECT i.message_index, i.filename, i.mime_type, b.content
            FROM conversation_message_images AS i
            JOIN conversation_blobs AS b ON b.blob_id = i.blob_id
            WHERE i.conversation_id = ?
            ORDER BY i.message_index, i.image_index
            """,
            (conversation_id,),
        ).fetchall()
        grouped: dict[int, list[ChatImage]] = {}
        for row in rows:
            grouped.setdefault(int(row["message_index"]), []).append(
                ChatImage(
                    filename=str(row["filename"]),
                    mime_type=str(row["mime_type"]),
                    content_base64=base64.b64encode(bytes(row["content"])).decode("ascii"),
                )
            )
        return grouped

    def _load_documents_locked(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
    ) -> dict[int, list[GeneratedChatDocument]]:
        rows = connection.execute(
            """
            SELECT d.message_index, d.filename, d.mime_type, d.title,
                   d.document_kind, d.source_url, b.content
            FROM conversation_message_documents AS d
            JOIN conversation_blobs AS b ON b.blob_id = d.blob_id
            WHERE d.conversation_id = ?
            ORDER BY d.message_index, d.document_index
            """,
            (conversation_id,),
        ).fetchall()
        grouped: dict[int, list[GeneratedChatDocument]] = {}
        for row in rows:
            grouped.setdefault(int(row["message_index"]), []).append(
                GeneratedChatDocument(
                    filename=str(row["filename"]),
                    mime_type=str(row["mime_type"]),
                    content_base64=base64.b64encode(bytes(row["content"])).decode("ascii"),
                    title=None if row["title"] is None else str(row["title"]),
                    document_kind=str(row["document_kind"]),
                    source_url=(
                        None if row["source_url"] is None else str(row["source_url"])
                    ),
                )
            )
        return grouped

    def _normalized_generated_documents(
        self,
        message: ConversationMessage,
    ) -> list[GeneratedChatDocument]:
        candidates: list[GeneratedChatDocument] = []
        if message.generated_document is not None:
            candidates.append(message.generated_document)
        candidates.extend(message.generated_documents)

        unique_documents: list[GeneratedChatDocument] = []
        seen: set[tuple[str, str, str, str | None, str, str | None]] = set()
        for document in candidates:
            key = (
                hashlib.sha256(document.content_base64.encode("utf-8")).hexdigest(),
                document.filename,
                document.mime_type,
                document.title,
                document.document_kind,
                document.source_url,
            )
            if key in seen:
                continue
            seen.add(key)
            unique_documents.append(document)
        return unique_documents

    def _message_fingerprint(
        self,
        message: ConversationMessage,
        *,
        citations_json: str,
        tool_trace_json: str,
        documents: list[GeneratedChatDocument],
    ) -> str:
        """Hash persisted fields so unchanged message prefixes are never rewritten."""
        digest = hashlib.sha256()

        def add(value: str | None) -> None:
            encoded = str(value or "").encode("utf-8")
            digest.update(len(encoded).to_bytes(8, byteorder="big"))
            digest.update(encoded)

        for value in (
            message.role,
            message.label,
            message.body,
            citations_json,
            tool_trace_json,
        ):
            add(value)
        add(str(len(message.images)))
        for image in message.images:
            add(image.filename)
            add(image.mime_type)
            add(hashlib.sha256(image.content_base64.encode("utf-8")).hexdigest())
        add(str(len(documents)))
        for document in documents:
            add(document.filename)
            add(document.mime_type)
            add(document.title)
            add(document.document_kind)
            add(document.source_url)
            add(hashlib.sha256(document.content_base64.encode("utf-8")).hexdigest())
        return digest.hexdigest()

    def _delete_orphaned_blobs_locked(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM conversation_blobs
            WHERE NOT EXISTS (
                SELECT 1 FROM conversation_message_images AS i
                WHERE i.blob_id = conversation_blobs.blob_id
            )
            AND NOT EXISTS (
                SELECT 1 FROM conversation_message_documents AS d
                WHERE d.blob_id = conversation_blobs.blob_id
            )
            """
        )

    @staticmethod
    def _decode_base64(value: str, *, label: str) -> bytes:
        try:
            return base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"Stored {label} is not valid base64.") from exc

    def _summary_from_row(self, row: sqlite3.Row) -> SavedConversationSummary:
        return SavedConversationSummary(
            conversation_id=str(row["conversation_id"]),
            owner_user_id=str(row["owner_user_id"]),
            title=None if row["title"] is None else str(row["title"]),
            title_is_custom=bool(row["title_is_custom"]),
            summary=str(row["summary"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            message_count=int(row["message_count"]),
            source_mode=str(row["source_mode"]),
            reasoning_mode=str(row["reasoning_mode"]),
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
        issue_hint = bool(
            re.search(
                r"\b(issue|problem|error|outage|failure)\b",
                summary,
                flags=re.IGNORECASE,
            )
        )
        summary = summary.split("```", 1)[0].strip()
        summary = (
            summary.split(":", 1)[-1].strip()
            if summary.lower().startswith("summary:")
            else summary
        )
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
            "maintain", "update", "create", "generate", "draft", "explain",
            "fix", "resolve", "rename", "delete", "move", "upload", "save",
            "open", "use", "find", "show", "review", "check",
        }:
            tokens = tokens[1:]

        compact_tokens: list[str] = []
        removable_words = {
            "the", "a", "an", "my", "our", "your", "their", "through",
            "with", "for", "into", "from", "that", "this", "these", "those",
            "part", "one", "it",
        }
        for token in tokens:
            if len(compact_tokens) >= 6:
                break
            if compact_tokens and token.lower() in removable_words:
                continue
            compact_tokens.append(token)

        if not compact_tokens:
            return ""

        if (
            issue_hint
            and compact_tokens[-1].lower()
            not in {"issue", "problem", "error", "outage", "failure"}
        ):
            if len(compact_tokens) >= 6:
                compact_tokens = compact_tokens[:5]
            compact_tokens.append("issue")

        summary = " ".join(compact_tokens).strip()
        if not summary:
            return ""
        summary = summary[0].upper() + summary[1:]
        return self._truncate_title(summary)

    @staticmethod
    def _truncate_title(value: str) -> str:
        max_length = 72
        if len(value) <= max_length:
            return value
        return f"{value[: max_length - 3].rstrip()}..."
