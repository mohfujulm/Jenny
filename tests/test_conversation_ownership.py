"""Verify saved conversations and chat mutations are isolated by owner."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from tests.main_runtime import main
from app.conversation_store import SavedConversationStore
from app.models import ConversationMessage, ConversationSaveRequest
from app.openai_agent import SessionManager
from app.user_store import UserStore


def _request_with_session(token: str | None = None) -> Request:
    headers = (
        [(b"cookie", f"{main.AUTH_COOKIE_NAME}={token}".encode("latin-1"))]
        if token
        else []
    )
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


class ConversationOwnershipTests(unittest.TestCase):
    def test_existing_conversations_are_migrated_to_default_administrator(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "saved_conversations.json"
            path.write_text(
                json.dumps(
                    {
                        "conversations": [
                            {
                                "conversation_id": "legacy-chat",
                                "title": None,
                                "title_is_custom": False,
                                "summary": "Legacy",
                                "created_at": "2026-01-01T00:00:00+00:00",
                                "updated_at": "2026-01-01T00:00:00+00:00",
                                "message_count": 1,
                                "source_mode": "broader",
                                "reasoning_mode": "standard",
                                "context_filter": {
                                    "folder_ids": [],
                                    "document_ids": [],
                                },
                                "messages": [
                                    {
                                        "role": "user",
                                        "label": "You",
                                        "body": "Legacy question",
                                        "citations": [],
                                        "tool_trace": [],
                                        "generated_document": None,
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            store = SavedConversationStore(
                path,
                default_owner_user_id="administrator-id",
            )

            self.assertEqual(
                store.get_conversation(
                    "legacy-chat",
                    "administrator-id",
                ).owner_user_id,
                "administrator-id",
            )
            self.assertIsNone(store.get_conversation("legacy-chat", "member-id"))

    def test_conversation_apis_are_filtered_by_signed_in_owner(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            users = UserStore(root / "application.sqlite")
            owner = users.create_user(
                username="owner",
                display_name="Owner",
                password="PortablePass1",
            )
            other = users.create_user(
                username="other",
                display_name="Other",
                password="PortablePass1",
            )
            owner_token = users.create_session(owner.user_id, 24)
            other_token = users.create_session(other.user_id, 24)
            conversations = SavedConversationStore(
                root / "saved_conversations.json",
                default_owner_user_id=owner.user_id,
            )
            manager = SessionManager(60, saved_conversations=conversations)
            session = manager.get_or_create("private-chat", owner.user_id)
            session.transcript.append(
                ConversationMessage(
                    role="user",
                    label="You",
                    body="Private question",
                )
            )
            manager.save_conversation("private-chat", owner.user_id)
            fresh_manager = SessionManager(60, saved_conversations=conversations)
            with self.assertRaises(PermissionError):
                fresh_manager.get_or_create("private-chat", other.user_id)

            with (
                patch.object(main, "user_store", users),
                patch.object(main, "session_manager", manager),
            ):
                owner_list = main.list_conversations(
                    _request_with_session(owner_token)
                )
                other_list = main.list_conversations(
                    _request_with_session(other_token)
                )
                with self.assertRaises(HTTPException) as cross_user_read:
                    main.get_conversation(
                        "private-chat",
                        _request_with_session(other_token),
                    )
                with self.assertRaises(HTTPException) as cross_user_write:
                    main.save_conversation(
                        ConversationSaveRequest(conversation_id="private-chat"),
                        _request_with_session(other_token),
                    )
                with self.assertRaises(HTTPException) as signed_out:
                    main.list_conversations(_request_with_session())

            self.assertEqual(
                [item.conversation_id for item in owner_list.conversations],
                ["private-chat"],
            )
            self.assertEqual(other_list.conversations, [])
            self.assertEqual(cross_user_read.exception.status_code, 404)
            self.assertEqual(cross_user_write.exception.status_code, 404)
            self.assertEqual(signed_out.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
