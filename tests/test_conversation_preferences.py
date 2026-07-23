from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.conversation_store import SavedConversationStore
from app.models import ContextFilter, ConversationMessage
from app.openai_agent import SessionManager


class ConversationPreferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.store = SavedConversationStore(
            Path(self.temp_dir.name) / "saved_conversations.json"
        )
        self.manager = SessionManager(60, saved_conversations=self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _add_user_message(self, conversation_id: str, body: str = "Test question") -> None:
        session = self.manager.get_or_create(conversation_id)
        session.history.append({"role": "user", "content": body})
        session.transcript.append(
            ConversationMessage(role="user", label="You", body=body)
        )

    def test_save_request_settings_are_written_with_the_conversation(self) -> None:
        conversation_id = "chat-save-settings"
        self._add_user_message(conversation_id)

        saved = self.manager.save_conversation(
            conversation_id,
            source_mode="broader",
            context_filter=ContextFilter(
                folder_ids=["Projects/PANYNJ"],
                document_ids=["DOC-123"],
            ),
        )

        self.assertEqual(saved.source_mode, "broader")
        self.assertEqual(saved.context_filter.folder_ids, ["Projects/PANYNJ"])
        self.assertEqual(saved.context_filter.document_ids, ["DOC-123"])

    def test_settings_update_persists_for_an_existing_saved_chat(self) -> None:
        conversation_id = "chat-settings-update"
        self._add_user_message(conversation_id)
        self.manager.save_conversation(conversation_id, title="PANYNJ notes")

        updated = self.manager.update_conversation_settings(
            conversation_id,
            "broader",
            ContextFilter(document_ids=["DOC-456"]),
        )

        self.assertIsNotNone(updated)
        reloaded = self.store.get_conversation(conversation_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.title, "PANYNJ notes")
        self.assertTrue(reloaded.title_is_custom)
        self.assertEqual(reloaded.source_mode, "broader")
        self.assertEqual(reloaded.context_filter.document_ids, ["DOC-456"])

    def test_unsaved_chat_settings_stay_on_the_session_until_first_save(self) -> None:
        conversation_id = "chat-unsaved-settings"

        updated = self.manager.update_conversation_settings(
            conversation_id,
            "broader",
            ContextFilter(folder_ids=["Project Delivery"]),
        )
        self.assertIsNone(updated)

        self._add_user_message(conversation_id)
        saved = self.manager.save_conversation(conversation_id)

        self.assertEqual(saved.source_mode, "broader")
        self.assertEqual(saved.context_filter.folder_ids, ["Project Delivery"])


if __name__ == "__main__":
    unittest.main()
