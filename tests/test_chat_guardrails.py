"""Exercise chat cancellation, deadlines, citation filtering, and safety limits."""

from __future__ import annotations

import json
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from app.datastore import DocumentRecord, RetrievalContext
from app.document_generator import GeneratedDocumentResult
from app.models import (
    ChatImage,
    Citation,
    ContextFilter,
    ConversationMessage,
    GeneratedChatDocument,
    SessionState,
)
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

    def test_generated_document_reuses_loaded_sources_and_skips_final_model_round(self) -> None:
        document = DocumentRecord(
            document_id="DOC-1",
            title="Project Notes",
            category="project",
            folder="Projects/Test",
            tags=["project"],
            summary="Project source material.",
            text="Relevant source text.",
        )
        self.agent._document_store.get_document.return_value = document
        generator = Mock()
        generator.generate_document.return_value = GeneratedDocumentResult(
            filename="project-summary.pdf",
            mime_type="application/pdf",
            content_bytes=b"%PDF-1.4\n%%EOF",
            message="Generated project-summary.pdf from 1 internal source.",
            citations=[
                Citation(
                    document_id="DOC-1",
                    title="Project Notes",
                    category="project",
                )
            ],
        )
        self.agent._document_generator = generator
        self.agent._settings.chat_max_tool_rounds = 5
        get_document_call = SimpleNamespace(
            type="function_call",
            name="get_document",
            arguments=json.dumps({"document_id": "DOC-1"}),
            call_id="get-1",
        )
        generate_call = SimpleNamespace(
            type="function_call",
            name="generate_context_document",
            arguments=json.dumps(
                {
                    "title": "Project Summary",
                    "output_format": "pdf",
                    "instructions": "Create a concise project summary.",
                }
            ),
            call_id="generate-1",
        )
        self.agent._client.responses.create.side_effect = [
            SimpleNamespace(output=[get_document_call], output_text=""),
            SimpleNamespace(output=[generate_call], output_text=""),
        ]

        response = self.agent.chat(
            "conversation-1",
            "user-1",
            "Create a downloadable project summary from DOC-1.",
            [],
            "internal",
            ContextFilter(),
            request_id="request-generation",
        )

        self.assertEqual(self.agent._client.responses.create.call_count, 2)
        self.assertEqual(response.generated_document.filename, "project-summary.pdf")
        self.assertEqual(
            response.assistant_message,
            "Generated project-summary.pdf from 1 internal source.",
        )
        self.assertEqual(
            generator.generate_document.call_args.kwargs["supporting_document_ids"],
            ["DOC-1"],
        )

    def test_explicit_file_request_bypasses_chat_planning_rounds(self) -> None:
        generator = Mock()
        generator.generate_document.return_value = GeneratedDocumentResult(
            filename="direct-summary.pdf",
            mime_type="application/pdf",
            content_bytes=b"%PDF-1.4\n%%EOF",
            message="Generated direct-summary.pdf.",
            citations=[],
        )
        self.agent._document_generator = generator

        response = self.agent.chat(
            "conversation-1",
            "user-1",
            "Create a PDF summary of the selected project files.",
            [],
            "internal",
            ContextFilter(),
            request_id="request-direct-generation",
        )

        self.agent._client.responses.create.assert_not_called()
        generator.generate_document.assert_called_once()
        self.assertEqual(response.generated_document.filename, "direct-summary.pdf")
        self.assertEqual(response.assistant_message, "Generated direct-summary.pdf.")

    def test_history_compaction_keeps_only_recent_bounded_turns(self) -> None:
        self.agent._settings.chat_history_max_messages = 4
        self.agent._settings.chat_history_max_chars = 2_500
        session = SessionState(
            conversation_id="conversation-history",
            owner_user_id="user-1",
            history=[{"role": "user", "content": "stale tool history"}],
            transcript=[
                ConversationMessage(
                    role="user" if index % 2 == 0 else "assistant",
                    label="You" if index % 2 == 0 else "Assistant",
                    body=f"message-{index}-" + ("x" * 990),
                )
                for index in range(8)
            ],
        )

        self.agent._compact_session_history(session)

        self.assertEqual(len(session.history), 2)
        self.assertTrue(session.history[0]["content"].startswith("message-6-"))
        self.assertTrue(session.history[1]["content"].startswith("message-7-"))

    def test_internal_search_results_and_full_document_payloads_are_bounded(self) -> None:
        self.agent._document_store.search_documents.return_value = []
        self.agent._execute_tool(
            "search_documents",
            {"query": "project", "limit": 8},
            "internal",
            RetrievalContext(),
        )
        self.assertEqual(
            self.agent._document_store.search_documents.call_args.kwargs["limit"],
            4,
        )

        self.agent._document_store.get_document.return_value = DocumentRecord(
            document_id="DOC-LONG",
            title="Long document",
            category="project",
            folder="Projects/Test",
            tags=[],
            summary="Long source.",
            text="x" * 20_000,
        )
        payload, _citations, _summary, _generated = self.agent._execute_tool(
            "get_document",
            {"document_id": "DOC-LONG"},
            "internal",
            RetrievalContext(),
        )
        self.assertEqual(len(payload["text"]), 6_000)

    def test_current_turn_reasoning_cannot_trigger_an_over_budget_second_call(self) -> None:
        self.agent._settings.chat_max_tool_rounds = 3
        self.agent._request_budget.maximum_units = 30_000
        oversized_reasoning = SimpleNamespace(
            type="reasoning",
            encrypted_content="r" * 40_000,
        )
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
            call_id="budget-call",
        )
        self.agent._client.responses.create.return_value = SimpleNamespace(
            output=[oversized_reasoning, tool_call],
            output_text="",
        )
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
            request_id="request-budget",
        )

        self.assertEqual(self.agent._client.responses.create.call_count, 1)
        self.assertIn("input budget", response.assistant_message)

    def test_current_images_use_bounded_high_detail(self) -> None:
        self.agent._client.responses.create.return_value = SimpleNamespace(
            output=[],
            output_text="Done.",
        )
        self.agent.chat(
            "conversation-1",
            "user-1",
            "Review this image.",
            [
                ChatImage(
                    filename="panel.png",
                    mime_type="image/png",
                    content_base64="aGVsbG8=",
                )
            ],
            "broader",
            ContextFilter(),
            request_id="request-image-detail",
        )

        request = self.agent._client.responses.create.call_args.kwargs
        image_item = request["input"][-1]["content"][1]
        self.assertEqual(image_item["detail"], "high")

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
