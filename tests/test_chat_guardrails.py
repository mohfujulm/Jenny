from __future__ import annotations

import json
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from app.models import Citation, ContextFilter, GeneratedChatDocument, SessionState
from app.openai_agent import (
    BusinessKnowledgeAgent,
    ChatCancelledError,
    DatasheetRetrievalResult,
)
from app.source_retrieval import RetrievedSourceDocument


def build_settings(**overrides):
    values = {
        "openai_api_key": "test-key",
        "openai_standard_model": "gpt-5.6-luna",
        "openai_maximum_model": "gpt-5.6-terra",
        "openai_standard_reasoning_effort": "medium",
        "openai_maximum_reasoning_effort": "max",
        "openai_text_verbosity": "medium",
        "openai_store_responses": False,
        "chat_request_timeout_seconds": 120,
        "chat_max_tool_rounds": 2,
        "openai_request_timeout_seconds": 60,
        "source_download_timeout_seconds": 20,
        "source_download_max_attempts": 1,
        "source_download_max_bytes": 20 * 1024 * 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ChatGuardrailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = Mock()
        self.sessions.get_or_create.return_value = SessionState(
            conversation_id="conversation-1",
            owner_user_id="user-1",
        )
        self.agent = BusinessKnowledgeAgent(
            build_settings(),
            Mock(),
            self.sessions,
        )
        self.agent._client = Mock()

    def _source_tool_response(self, call_id: str):
        tool_call = SimpleNamespace(
            type="function_call",
            name="retrieve_source_pdf",
            arguments=json.dumps(
                {
                    "url": "https://manufacturer.example/datasheet.pdf",
                    "filename": "datasheet.pdf",
                    "title": "Datasheet",
                }
            ),
            call_id=call_id,
        )
        return SimpleNamespace(output=[tool_call], output_text="")

    def test_bounds_tool_rounds_and_source_download_attempts(self) -> None:
        self.agent._client.responses.create.side_effect = [
            self._source_tool_response("call-1"),
            self._source_tool_response("call-2"),
        ]
        self.agent._source_retriever = Mock()
        self.agent._source_retriever.retrieve_pdf.return_value = RetrievedSourceDocument(
            filename="datasheet.pdf",
            mime_type="application/pdf",
            content_bytes=b"%PDF-1.4\nsource\n%%EOF",
            source_url="https://manufacturer.example/datasheet.pdf",
        )

        response = self.agent.chat(
            "conversation-1",
            "user-1",
            "Find the original datasheet.",
            [],
            "broader",
            ContextFilter(),
            request_id="request-1",
        )

        self.assertEqual(self.agent._client.responses.create.call_count, 2)
        self.agent._source_retriever.retrieve_pdf.assert_called_once()
        self.assertIn("maximum number", response.assistant_message)

    def test_model_call_receives_a_bounded_timeout(self) -> None:
        self.agent._client.responses.create.return_value = SimpleNamespace(
            output=[],
            output_text="Done.",
        )

        self.agent.chat(
            "conversation-1",
            "user-1",
            "Hello",
            [],
            "broader",
            ContextFilter(),
            request_id="request-2",
        )

        timeout = self.agent._client.responses.create.call_args.kwargs["timeout"]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 60)

    def test_cancel_request_signals_the_active_chat(self) -> None:
        entered = threading.Event()
        result: dict[str, Exception] = {}

        def wait_for_cancel(*args):
            cancellation = args[-1]
            entered.set()
            cancellation.wait(timeout=2)
            if cancellation.is_set():
                raise ChatCancelledError("Response cancelled by the user.")
            raise AssertionError("Cancellation was not signalled.")

        self.agent._chat_impl = wait_for_cancel

        def run_chat() -> None:
            try:
                self.agent.chat(
                    "conversation-1",
                    "user-1",
                    "Hello",
                    [],
                    "broader",
                    ContextFilter(),
                    request_id="request-3",
                )
            except Exception as exc:
                result["error"] = exc

        worker = threading.Thread(target=run_chat)
        worker.start()
        self.assertTrue(entered.wait(timeout=1))
        self.assertTrue(self.agent.cancel_request("request-3", "user-1"))
        worker.join(timeout=2)

        self.assertIsInstance(result.get("error"), ChatCancelledError)
        self.assertFalse(self.agent.cancel_request("request-3", "user-1"))

    def test_cancel_releases_a_blocking_model_wait_and_rolls_back_turn(self) -> None:
        entered = threading.Event()
        release_model_worker = threading.Event()
        result: dict[str, Exception] = {}

        def blocking_model_call(**_kwargs):
            entered.set()
            release_model_worker.wait(timeout=2)
            return SimpleNamespace(output=[], output_text="Stale response")

        self.agent._client.responses.create.side_effect = blocking_model_call

        def run_chat() -> None:
            try:
                self.agent.chat(
                    "conversation-1",
                    "user-1",
                    "Cancel this request.",
                    [],
                    "broader",
                    ContextFilter(),
                    request_id="request-4",
                )
            except Exception as exc:
                result["error"] = exc

        worker = threading.Thread(target=run_chat)
        worker.start()
        self.assertTrue(entered.wait(timeout=1))
        self.assertTrue(self.agent.cancel_request("request-4", "user-1"))
        worker.join(timeout=0.75)
        release_model_worker.set()

        self.assertFalse(worker.is_alive())
        self.assertIsInstance(result.get("error"), ChatCancelledError)
        session = self.sessions.get_or_create.return_value
        self.assertEqual(session.history, [])
        self.assertEqual(session.transcript, [])
        self.agent._client.close.assert_called()

    def test_detects_and_deduplicates_multi_product_datasheet_request(self) -> None:
        products = self.agent._extract_datasheet_products(
            "Get datasheets for PNM-C12083RVD, PNM-C32083RQZ, "
            "PNM-C12083RVD, and XNV-A8016R."
        )

        self.assertEqual(
            products,
            ["PNM-C12083RVD", "PNM-C32083RQZ", "XNV-A8016R"],
        )

    def test_multi_product_request_returns_partial_successes_as_multiple_files(self) -> None:
        first_document = GeneratedChatDocument(
            filename="PNM-C12083RVD.pdf",
            mime_type="application/pdf",
            content_base64="JVBERi0=",
            title="PNM-C12083RVD datasheet",
            document_kind="source",
            source_url="https://example.com/one.pdf",
        )
        second_document = GeneratedChatDocument(
            filename="XNV-A8016R.pdf",
            mime_type="application/pdf",
            content_base64="JVBERi0=",
            title="XNV-A8016R datasheet",
            document_kind="source",
            source_url="https://example.com/two.pdf",
        )

        def retrieve(product, *_args):
            if product == "PNM-C32083RQZ":
                return DatasheetRetrievalResult(
                    product=product,
                    document=None,
                    citation=None,
                    detail="Exact PDF was not attached",
                )
            document = (
                first_document if product == "PNM-C12083RVD" else second_document
            )
            return DatasheetRetrievalResult(
                product=product,
                document=document,
                citation=Citation(
                    document_id=f"WEB-{product}",
                    title=document.title,
                    category="web",
                    source_url=document.source_url,
                ),
                detail="Retrieved",
            )

        self.agent._retrieve_single_datasheet = Mock(side_effect=retrieve)
        self.agent._create_openai_client = Mock(return_value=Mock())

        response = self.agent.chat(
            "conversation-1",
            "user-1",
            (
                "Get datasheets for PNM-C12083RVD, PNM-C32083RQZ, "
                "and XNV-A8016R."
            ),
            [],
            "broader",
            ContextFilter(),
            request_id="request-5",
        )

        self.assertEqual(
            [document.filename for document in response.generated_documents],
            ["PNM-C12083RVD.pdf", "XNV-A8016R.pdf"],
        )
        self.assertEqual(response.generated_document.filename, "PNM-C12083RVD.pdf")
        self.assertIn("2 of 3", response.assistant_message)
        self.assertIn("Exact PDF was not attached", response.assistant_message)
        transcript = self.sessions.get_or_create.return_value.transcript
        self.assertEqual(len(transcript[-1].generated_documents), 2)


if __name__ == "__main__":
    unittest.main()
