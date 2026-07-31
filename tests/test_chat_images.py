from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from app.conversation_store import SavedConversationStore
from app.models import ChatImage, ChatRequest, ConversationMessage
from app.openai_agent import SessionManager


_TINY_PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n" + b"test-image-content"
).decode("ascii")


class ChatImageTests(unittest.TestCase):
    def test_chat_accepts_an_image_without_text(self) -> None:
        request = ChatRequest(
            images=[
                ChatImage(
                    filename="panel.png",
                    mime_type="image/png",
                    content_base64=_TINY_PNG,
                )
            ]
        )

        self.assertEqual(request.message, "")
        self.assertEqual(request.images[0].filename, "panel.png")

    def test_chat_requires_text_or_an_image(self) -> None:
        with self.assertRaises(ValidationError):
            ChatRequest(message="   ")

    def test_rejects_invalid_image_content_and_unsupported_types(self) -> None:
        invalid_cases = [
            {
                "filename": "broken.png",
                "mime_type": "image/png",
                "content_base64": "not-base64!",
            },
            {
                "filename": "vector.svg",
                "mime_type": "image/svg+xml",
                "content_base64": _TINY_PNG,
            },
        ]
        for values in invalid_cases:
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    ChatImage(**values)

    def test_saved_images_restore_into_multimodal_model_history(self) -> None:
        with TemporaryDirectory() as directory:
            store = SavedConversationStore(
                Path(directory) / "saved_conversations.json",
                default_owner_user_id="owner-id",
            )
            manager = SessionManager(60, saved_conversations=store)
            session = manager.get_or_create("image-chat", "owner-id")
            session.transcript.append(
                ConversationMessage(
                    role="user",
                    label="You",
                    body="What is shown here?",
                    images=[
                        ChatImage(
                            filename="panel.png",
                            mime_type="image/png",
                            content_base64=_TINY_PNG,
                        )
                    ],
                )
            )
            manager.save_conversation("image-chat", "owner-id")

            restored_manager = SessionManager(60, saved_conversations=store)
            restored = restored_manager.get_or_create("image-chat", "owner-id")

            self.assertEqual(restored.transcript[0].images[0].filename, "panel.png")
            self.assertEqual(restored.history[0]["content"][0]["type"], "input_text")
            self.assertEqual(restored.history[0]["content"][1]["type"], "input_image")
            self.assertTrue(
                restored.history[0]["content"][1]["image_url"].startswith(
                    "data:image/png;base64,"
                )
            )


if __name__ == "__main__":
    unittest.main()
