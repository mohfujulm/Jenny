"""Verify privacy-safe local attribution for paid OpenAI operations."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.datastore import (
    RetrievalContext,
    SemanticDocumentStore,
    _embed_texts_in_batches,
)
from app.document_generator import ContextDocumentGenerator
from app.openai_usage import record_openai_usage
from app.openai_agent import BusinessKnowledgeAgent
from app.pdf_vision import PdfVisionAnalyzer, PdfVisionPage


def _read_events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class OpenAIUsageTests(unittest.TestCase):
    @staticmethod
    def _reasoning_settings() -> SimpleNamespace:
        return SimpleNamespace(
            openai_api_key="test-key",
            openai_standard_model="gpt-standard-test",
            openai_maximum_model="gpt-maximum-test",
            openai_standard_reasoning_effort="medium",
            openai_maximum_reasoning_effort="max",
            openai_text_verbosity="medium",
            openai_store_responses=False,
            chat_request_timeout_seconds=120,
            chat_max_tool_rounds=5,
            openai_request_timeout_seconds=60,
            source_download_timeout_seconds=30,
            source_download_max_attempts=1,
            source_download_max_bytes=20 * 1024 * 1024,
        )

    def test_records_token_counts_request_id_and_safe_error_metadata(self) -> None:
        class FakeApiError(RuntimeError):
            status_code = 429
            code = "rate_limit_exceeded"
            request_id = "req_error_123"

        with TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "usage.jsonl"
            with patch.dict(
                os.environ,
                {"OPENAI_USAGE_LOG_PATH": str(log_path)},
                clear=False,
            ):
                self.assertTrue(
                    record_openai_usage(
                        operation="responses.create",
                        purpose="pdf_vision",
                        model="vision-model",
                        response=SimpleNamespace(
                            usage=SimpleNamespace(
                                input_tokens=120,
                                output_tokens=30,
                                total_tokens=150,
                            ),
                            _request_id="req_success_123",
                        ),
                        item_count=2,
                        page_count=2,
                    )
                )
                self.assertTrue(
                    record_openai_usage(
                        operation="embeddings.create",
                        purpose="semantic_index_sync_search",
                        model="embedding-model",
                        error=FakeApiError(
                            "sk-test-secret and confidential document contents"
                        ),
                        item_count=3,
                        chunk_count=3,
                    )
                )

            serialized = log_path.read_text(encoding="utf-8")
            events = _read_events(log_path)
            self.assertEqual(len(events), 2)
            self.assertTrue(str(events[0]["timestamp"]).endswith("Z"))
            self.assertEqual(events[0]["input_tokens"], 120)
            self.assertEqual(events[0]["output_tokens"], 30)
            self.assertEqual(events[0]["total_tokens"], 150)
            self.assertEqual(events[0]["request_id"], "req_success_123")
            self.assertEqual(events[0]["page_count"], 2)
            self.assertEqual(events[1]["status"], "error")
            self.assertEqual(
                events[1]["error"],
                {
                    "type": "FakeApiError",
                    "status_code": 429,
                    "code": "rate_limit_exceeded",
                },
            )
            self.assertEqual(events[1]["request_id"], "req_error_123")
            self.assertNotIn("sk-test-secret", serialized)
            self.assertNotIn("confidential document contents", serialized)

    def test_pdf_vision_attributes_each_paid_batch(self) -> None:
        settings = SimpleNamespace(
            openai_api_key="test-key",
            openai_store_responses=False,
            pdf_vision_model="gpt-vision-test",
            pdf_vision_batch_size=3,
            pdf_vision_timeout_seconds=60,
        )
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            output_text=(
                '{"pages":[{"page_number":4,'
                '"visual_summary":"A labeled equipment diagram."}]}'
            ),
            usage=SimpleNamespace(
                input_tokens=400,
                output_tokens=25,
                total_tokens=425,
            ),
            _request_id="req_vision_123",
        )

        with TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "usage.jsonl"
            with patch.dict(
                os.environ,
                {"OPENAI_USAGE_LOG_PATH": str(log_path)},
                clear=False,
            ):
                result = PdfVisionAnalyzer(settings, client=client).analyze_pages(
                    [
                        PdfVisionPage(
                            page_number=4,
                            image_bytes=b"jpeg",
                            native_text="sensitive native text",
                            ocr_text="sensitive OCR text",
                        )
                    ]
                )

            self.assertEqual(result, {4: "A labeled equipment diagram."})
            events = _read_events(log_path)
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["operation"], "responses.create")
            self.assertEqual(event["purpose"], "pdf_vision")
            self.assertEqual(event["model"], "gpt-vision-test")
            self.assertEqual(event["item_count"], 1)
            self.assertEqual(event["page_count"], 1)
            self.assertEqual(event["total_tokens"], 425)
            serialized = log_path.read_text(encoding="utf-8")
            self.assertNotIn("sensitive native text", serialized)
            self.assertNotIn("sensitive OCR text", serialized)

    def test_embedding_batches_attribute_chunks_and_embedding_usage(self) -> None:
        client = Mock()
        client.embeddings.create.side_effect = [
            SimpleNamespace(
                data=[
                    SimpleNamespace(embedding=[0.1]),
                    SimpleNamespace(embedding=[0.2]),
                ],
                usage=SimpleNamespace(prompt_tokens=20, total_tokens=20),
                _request_id="req_embed_1",
            ),
            SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.3])],
                usage=SimpleNamespace(prompt_tokens=7, total_tokens=7),
                _request_id="req_embed_2",
            ),
        ]

        with TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "usage.jsonl"
            with patch.dict(
                os.environ,
                {"OPENAI_USAGE_LOG_PATH": str(log_path)},
                clear=False,
            ):
                embeddings = _embed_texts_in_batches(
                    client=client,
                    texts=["private chunk one", "private chunk two", "private chunk three"],
                    model="text-embedding-test",
                    dimensions=None,
                    batch_size=2,
                    purpose="semantic_index_sync_search",
                )

            self.assertEqual(embeddings, [[0.1], [0.2], [0.3]])
            events = _read_events(log_path)
            self.assertEqual([event["chunk_count"] for event in events], [2, 1])
            self.assertEqual([event["input_tokens"] for event in events], [20, 7])
            self.assertTrue(
                all(event["purpose"] == "semantic_index_sync_search" for event in events)
            )
            serialized = log_path.read_text(encoding="utf-8")
            self.assertNotIn("private chunk", serialized)

    def test_chat_and_datasheet_responses_have_distinct_private_attribution(self) -> None:
        class FakeApiError(RuntimeError):
            status_code = 503
            request_id = "req_datasheet_error"

        agent = BusinessKnowledgeAgent(self._reasoning_settings(), Mock(), Mock())
        agent._client = Mock()
        agent._client.responses.create.side_effect = [
            SimpleNamespace(
                output=[],
                usage=SimpleNamespace(
                    input_tokens=90,
                    output_tokens=12,
                    total_tokens=102,
                ),
                _request_id="req_chat_success",
            ),
            FakeApiError("private datasheet request and sk-test-secret"),
        ]

        with TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "usage.jsonl"
            with patch.dict(
                os.environ,
                {"OPENAI_USAGE_LOG_PATH": str(log_path)},
                clear=False,
            ):
                agent._run_response(
                    [{"role": "user", "content": "private chat prompt"}],
                    "internal",
                    RetrievalContext(),
                )
                with self.assertRaises(FakeApiError):
                    agent._run_response(
                        [{"role": "user", "content": "private datasheet prompt"}],
                        "broader",
                        RetrievalContext(),
                        purpose="datasheet_retrieval",
                    )

            events = _read_events(log_path)
            self.assertEqual(
                [event["purpose"] for event in events],
                ["chat_response", "datasheet_retrieval"],
            )
            self.assertEqual(events[0]["total_tokens"], 102)
            self.assertEqual(events[0]["item_count"], 1)
            self.assertEqual(events[1]["status"], "error")
            self.assertEqual(events[1]["request_id"], "req_datasheet_error")
            serialized = log_path.read_text(encoding="utf-8")
            self.assertNotIn("private chat prompt", serialized)
            self.assertNotIn("private datasheet prompt", serialized)
            self.assertNotIn("sk-test-secret", serialized)

    def test_document_generation_attributes_text_and_workbook_requests(self) -> None:
        generator = ContextDocumentGenerator(self._reasoning_settings(), Mock())
        generator._client = Mock()
        generator._client.responses.create.side_effect = [
            SimpleNamespace(
                output_text="Generated body",
                usage=SimpleNamespace(
                    input_tokens=80,
                    output_tokens=20,
                    total_tokens=100,
                ),
                _request_id="req_pdf_generation",
            ),
            SimpleNamespace(
                output_text=(
                    '{"workbook_title":"Report","sheets":['
                    '{"name":"Data","rows":[["Header"],["Value"]]}]}'
                ),
                usage=SimpleNamespace(
                    input_tokens=70,
                    output_tokens=30,
                    total_tokens=100,
                ),
                _request_id="req_xlsx_generation",
            ),
        ]
        supporting_document = Mock()
        supporting_document.to_tool_payload.return_value = {
            "text": "private supporting document text"
        }

        with TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "usage.jsonl"
            with patch.dict(
                os.environ,
                {"OPENAI_USAGE_LOG_PATH": str(log_path)},
                clear=False,
            ):
                generated_text = generator._generate_text_document(
                    title="Private report title",
                    instructions="private document instructions",
                    output_format="pdf",
                    source_mode="internal",
                    reasoning_mode="standard",
                    retrieval_context=RetrievalContext(),
                    supporting_documents=[supporting_document],
                    citations=[],
                )
                workbook_bytes = generator._generate_workbook_bytes(
                    title="Private workbook title",
                    instructions="private workbook instructions",
                    source_mode="internal",
                    reasoning_mode="standard",
                    retrieval_context=RetrievalContext(),
                    supporting_documents=[supporting_document],
                    citations=[],
                )

            self.assertEqual(generated_text, "Generated body")
            self.assertTrue(workbook_bytes.startswith(b"PK"))
            events = _read_events(log_path)
            self.assertEqual(
                [event["purpose"] for event in events],
                ["document_generation_pdf", "document_generation_xlsx"],
            )
            self.assertEqual([event["item_count"] for event in events], [1, 1])
            serialized = log_path.read_text(encoding="utf-8")
            self.assertNotIn("private document instructions", serialized)
            self.assertNotIn("private supporting document text", serialized)
            self.assertNotIn("Private workbook title", serialized)

    def test_semantic_query_embeddings_have_search_and_answer_purposes(self) -> None:
        store = SemanticDocumentStore(
            Path("unused-semantic-index.sqlite"),
            "test-key",
            "text-search-test",
            None,
            "text-answer-test",
            None,
        )
        store._ensure_loaded = Mock()
        store._chunks = []
        store._client = Mock()
        store._client.embeddings.create.side_effect = [
            SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1])],
                usage=SimpleNamespace(prompt_tokens=4, total_tokens=4),
                _request_id="req_query_search",
            ),
            SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.2])],
                usage=SimpleNamespace(prompt_tokens=5, total_tokens=5),
                _request_id="req_query_answer",
            ),
        ]

        with TemporaryDirectory() as temporary_directory:
            log_path = Path(temporary_directory) / "usage.jsonl"
            with patch.dict(
                os.environ,
                {"OPENAI_USAGE_LOG_PATH": str(log_path)},
                clear=False,
            ):
                self.assertEqual(
                    store.search_documents("private search query", search_profile="search"),
                    [],
                )
                self.assertEqual(
                    store.search_documents("private answer query", search_profile="answer"),
                    [],
                )

            events = _read_events(log_path)
            self.assertEqual(
                [event["purpose"] for event in events],
                ["semantic_query_search", "semantic_query_answer"],
            )
            self.assertEqual(
                [event["model"] for event in events],
                ["text-search-test", "text-answer-test"],
            )
            self.assertEqual([event["item_count"] for event in events], [1, 1])
            self.assertTrue(all("chunk_count" not in event for event in events))
            serialized = log_path.read_text(encoding="utf-8")
            self.assertNotIn("private search query", serialized)
            self.assertNotIn("private answer query", serialized)


if __name__ == "__main__":
    unittest.main()
