"""Exercise atomic migration and normalized SQLite conversation storage."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from app.conversation_store import SavedConversationStore
from app.models import (
    ChatImage,
    ConversationMessage,
    GeneratedChatDocument,
    SessionState,
)


class ConversationSQLiteStoreTests(unittest.TestCase):
    def _counts(self, database_path: Path) -> dict[str, int]:
        with closing(sqlite3.connect(database_path)) as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "conversations",
                    "conversation_messages",
                    "conversation_blobs",
                    "conversation_message_images",
                    "conversation_message_documents",
                )
            }

    def test_legacy_json_migrates_once_without_rewriting_source_or_duplicate_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_path = root / "saved_conversations.json"
            database_path = root / "saved_conversations.sqlite"
            image_base64 = base64.b64encode(b"raw-image-bytes").decode("ascii")
            pdf_base64 = base64.b64encode(b"%PDF-1.4\ncontent\n%%EOF").decode("ascii")
            generated_document = {
                "filename": "project-summary.pdf",
                "mime_type": "application/pdf",
                "content_base64": pdf_base64,
                "title": "Project Summary",
                "document_kind": "generated",
                "source_url": None,
            }
            legacy_payload = {
                "conversations": [
                    {
                        "conversation_id": "legacy-chat",
                        "title": "Legacy project",
                        "title_is_custom": True,
                        "summary": "Legacy project",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-02T00:00:00+00:00",
                        "message_count": 2,
                        "source_mode": "broader",
                        "reasoning_mode": "standard",
                        "context_filter": {"folder_ids": [], "document_ids": []},
                        "messages": [
                            {
                                "role": "user",
                                "label": "You",
                                "body": "Review this image.",
                                "images": [
                                    {
                                        "filename": "diagram.png",
                                        "mime_type": "image/png",
                                        "content_base64": image_base64,
                                    }
                                ],
                                "citations": [],
                                "tool_trace": [],
                            },
                            {
                                "role": "assistant",
                                "label": "Assistant",
                                "body": "Created the project summary.",
                                "citations": [],
                                "tool_trace": [],
                                # The legacy and current fields contain the same file.
                                "generated_document": generated_document,
                                "generated_documents": [generated_document],
                            },
                        ],
                    }
                ]
            }
            legacy_bytes = json.dumps(legacy_payload, indent=2).encode("utf-8")
            legacy_path.write_bytes(legacy_bytes)

            store = SavedConversationStore(
                database_path,
                default_owner_user_id="administrator-id",
                legacy_json_path=legacy_path,
            )
            loaded = store.get_conversation("legacy-chat", "administrator-id")

            self.assertIsNotNone(loaded)
            self.assertEqual(legacy_path.read_bytes(), legacy_bytes)
            self.assertEqual(loaded.messages[0].images[0].content_base64, image_base64)
            self.assertEqual(len(loaded.messages[1].generated_documents), 1)
            self.assertIsNone(loaded.messages[1].generated_document)
            self.assertEqual(
                loaded.messages[1].generated_documents[0].content_base64,
                pdf_base64,
            )
            self.assertEqual(
                self._counts(database_path),
                {
                    "conversations": 1,
                    "conversation_messages": 2,
                    "conversation_blobs": 2,
                    "conversation_message_images": 1,
                    "conversation_message_documents": 1,
                },
            )

            # Reopening neither reimports nor mutates the recovery JSON.
            SavedConversationStore(
                database_path,
                default_owner_user_id="administrator-id",
                legacy_json_path=legacy_path,
            )
            self.assertEqual(self._counts(database_path)["conversations"], 1)
            self.assertEqual(legacy_path.read_bytes(), legacy_bytes)

    def test_newer_transition_json_is_reconciled_without_overwriting_newer_sqlite(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_path = root / "saved_conversations.json"
            database_path = root / "saved_conversations.sqlite"

            def write_legacy(body: str, updated_at: str) -> None:
                legacy_path.write_text(
                    json.dumps(
                        {
                            "conversations": [
                                {
                                    "conversation_id": "transition-chat",
                                    "owner_user_id": "owner-1",
                                    "title": None,
                                    "title_is_custom": False,
                                    "summary": body,
                                    "created_at": "2026-01-01T00:00:00+00:00",
                                    "updated_at": updated_at,
                                    "message_count": 1,
                                    "source_mode": "broader",
                                    "reasoning_mode": "standard",
                                    "context_filter": {
                                        "folder_ids": [],
                                        "document_ids": [],
                                    },
                                    "messages": [
                                        {"role": "user", "label": "You", "body": body}
                                    ],
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

            write_legacy("Initial JSON message", "2026-01-01T00:00:00+00:00")
            SavedConversationStore(
                database_path,
                default_owner_user_id="owner-1",
                legacy_json_path=legacy_path,
            )

            # This represents one final write by an old process during rollout.
            write_legacy("Newer JSON message", "2026-02-01T00:00:00+00:00")
            reconciled_store = SavedConversationStore(
                database_path,
                default_owner_user_id="owner-1",
                legacy_json_path=legacy_path,
            )
            self.assertEqual(
                reconciled_store.get_conversation(
                    "transition-chat",
                    "owner-1",
                ).messages[0].body,
                "Newer JSON message",
            )

            sqlite_session = SessionState(
                conversation_id="transition-chat",
                owner_user_id="owner-1",
                transcript=[
                    ConversationMessage(
                        role="user",
                        label="You",
                        body="Newest SQLite message",
                    )
                ],
            )
            reconciled_store.save_session(sqlite_session)
            write_legacy("Stale changed JSON", "2026-03-01T00:00:00+00:00")
            final_store = SavedConversationStore(
                database_path,
                default_owner_user_id="owner-1",
                legacy_json_path=legacy_path,
            )
            self.assertEqual(
                final_store.get_conversation(
                    "transition-chat",
                    "owner-1",
                ).messages[0].body,
                "Newest SQLite message",
            )

    def test_interrupted_save_rolls_back_the_entire_conversation(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "saved_conversations.sqlite"
            store = SavedConversationStore(
                database_path,
                default_owner_user_id="owner-1",
            )
            session = SessionState(
                conversation_id="atomic-chat",
                owner_user_id="owner-1",
                transcript=[
                    ConversationMessage(
                        role="user",
                        label="You",
                        body="Original persisted question",
                    )
                ],
            )
            store.save_session(session, title="Original title")

            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    """
                    CREATE TRIGGER simulate_interrupted_write
                    BEFORE INSERT ON conversation_messages
                    WHEN NEW.body = 'Simulated interrupted write'
                    BEGIN
                        SELECT RAISE(ABORT, 'simulated interruption');
                    END
                    """
                )
                connection.commit()

            session.transcript[0].body = "Simulated interrupted write"
            with self.assertRaises(sqlite3.IntegrityError):
                store.save_session(session, title="Title that must roll back")

            reloaded = store.get_conversation("atomic-chat", "owner-1")
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.title, "Original title")
            self.assertEqual(reloaded.messages[0].body, "Original persisted question")
            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )

    def test_metadata_only_save_does_not_rewrite_unchanged_messages(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "saved_conversations.sqlite"
            store = SavedConversationStore(
                database_path,
                default_owner_user_id="owner-1",
            )
            session = SessionState(
                conversation_id="metadata-chat",
                owner_user_id="owner-1",
                transcript=[
                    ConversationMessage(
                        role="user",
                        label="You",
                        body="Do not rewrite this message for a title update.",
                    )
                ],
            )
            store.save_session(session, title="First title")

            with closing(sqlite3.connect(database_path)) as connection:
                connection.executescript(
                    """
                    CREATE TRIGGER reject_message_insert
                    BEFORE INSERT ON conversation_messages
                    BEGIN
                        SELECT RAISE(ABORT, 'message insert was not expected');
                    END;
                    CREATE TRIGGER reject_message_delete
                    BEFORE DELETE ON conversation_messages
                    BEGIN
                        SELECT RAISE(ABORT, 'message delete was not expected');
                    END;
                    """
                )
                connection.commit()

            updated = store.save_session(session, title="Updated title")
            self.assertEqual(updated.title, "Updated title")
            self.assertEqual(updated.messages[0].body, session.transcript[0].body)

    def test_identical_binary_content_is_shared_and_garbage_collected(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "saved_conversations.sqlite"
            store = SavedConversationStore(
                database_path,
                default_owner_user_id="owner-1",
            )
            shared_content = base64.b64encode(b"shared-binary-content").decode("ascii")
            document = GeneratedChatDocument(
                filename="shared.pdf",
                mime_type="application/pdf",
                content_base64=shared_content,
            )
            session = SessionState(
                conversation_id="deduplicated-chat",
                owner_user_id="owner-1",
                transcript=[
                    ConversationMessage(
                        role="user",
                        label="You",
                        body="Use this image.",
                        images=[
                            ChatImage(
                                filename="shared.png",
                                mime_type="image/png",
                                content_base64=shared_content,
                            )
                        ],
                    ),
                    ConversationMessage(
                        role="assistant",
                        label="Assistant",
                        body="Created a file.",
                        generated_document=document,
                        generated_documents=[document],
                    ),
                ],
            )

            store.save_session(session)
            counts = self._counts(database_path)
            self.assertEqual(counts["conversation_blobs"], 1)
            self.assertEqual(counts["conversation_message_documents"], 1)
            self.assertTrue(store.delete_conversation("deduplicated-chat", "owner-1"))
            self.assertEqual(self._counts(database_path)["conversation_blobs"], 0)

    def test_parallel_conversation_saves_are_serialized_without_data_loss(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "saved_conversations.sqlite"
            stores = [
                SavedConversationStore(
                    database_path,
                    default_owner_user_id="owner-1",
                )
                for _index in range(4)
            ]

            def save_conversation(index: int) -> None:
                session = SessionState(
                    conversation_id=f"parallel-{index}",
                    owner_user_id="owner-1",
                    transcript=[
                        ConversationMessage(
                            role="user",
                            label="You",
                            body=f"Parallel question {index}",
                        )
                    ],
                )
                stores[index % len(stores)].save_session(session)

            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(save_conversation, range(12)))

            conversations = stores[0].list_conversations("owner-1")
            self.assertEqual(len(conversations), 12)
            self.assertEqual(
                {conversation.conversation_id for conversation in conversations},
                {f"parallel-{index}" for index in range(12)},
            )
            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                    "wal",
                )
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )


if __name__ == "__main__":
    unittest.main()
