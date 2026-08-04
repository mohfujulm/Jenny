"""Test public-PDF retrieval, redirects, limits, and SSRF protections."""

from __future__ import annotations

import socket
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from app.datastore import RetrievalContext
from app.openai_agent import BusinessKnowledgeAgent
from app.source_retrieval import RetrievedSourceDocument, SourceDocumentRetriever


PUBLIC_DNS_RESULT = [
    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
]


class SourceDocumentRetrieverTests(unittest.TestCase):
    def _retriever(self, handler, *, max_bytes: int = 4096) -> SourceDocumentRetriever:
        return SourceDocumentRetriever(
            timeout_seconds=5,
            max_bytes=max_bytes,
            transport=httpx.MockTransport(handler),
        )

    @patch("app.source_retrieval.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT)
    def test_preserves_original_pdf_bytes(self, _getaddrinfo: Mock) -> None:
        original = b"%PDF-1.7\noriginal source bytes\x00\xff\n%%EOF"
        retriever = self._retriever(
            lambda request: httpx.Response(
                200,
                headers={"content-disposition": 'attachment; filename="datasheet.pdf"'},
                content=original,
                request=request,
            )
        )

        result = retriever.retrieve_pdf("https://manufacturer.example/files/source")

        self.assertEqual(result.content_bytes, original)
        self.assertEqual(result.filename, "datasheet.pdf")
        self.assertEqual(result.source_url, "https://manufacturer.example/files/source")

    def test_rejects_non_https_and_private_hosts(self) -> None:
        retriever = self._retriever(lambda request: httpx.Response(200, request=request))

        with self.assertRaisesRegex(ValueError, "public HTTPS"):
            retriever.retrieve_pdf("http://example.com/file.pdf")
        with self.assertRaisesRegex(ValueError, "Private or local"):
            retriever.retrieve_pdf("https://127.0.0.1/file.pdf")

    @patch("app.source_retrieval.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT)
    def test_rejects_non_pdf_content(self, _getaddrinfo: Mock) -> None:
        retriever = self._retriever(
            lambda request: httpx.Response(200, content=b"<html>not a PDF</html>", request=request)
        )

        with self.assertRaisesRegex(ValueError, "not an original PDF"):
            retriever.retrieve_pdf("https://example.com/file.pdf")

    @patch("app.source_retrieval.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT)
    def test_rejects_documents_above_size_limit(self, _getaddrinfo: Mock) -> None:
        retriever = self._retriever(
            lambda request: httpx.Response(
                200,
                content=b"%PDF-" + (b"x" * 2048),
                request=request,
            ),
            max_bytes=1024,
        )

        with self.assertRaisesRegex(ValueError, "exceeds"):
            retriever.retrieve_pdf("https://example.com/file.pdf")

    def test_revalidates_redirect_destination(self) -> None:
        def resolve(host: str, *_args, **_kwargs):
            if host == "example.com":
                return PUBLIC_DNS_RESULT
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))
            ]

        retriever = self._retriever(
            lambda request: httpx.Response(
                302,
                headers={"location": "https://private.example/file.pdf"},
                request=request,
            )
        )

        with patch("app.source_retrieval.socket.getaddrinfo", side_effect=resolve):
            with self.assertRaisesRegex(ValueError, "Private or local"):
                retriever.retrieve_pdf("https://example.com/file.pdf")

    @patch("app.source_retrieval.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT)
    def test_enforces_total_download_deadline_while_streaming(
        self,
        _getaddrinfo: Mock,
    ) -> None:
        retriever = self._retriever(
            lambda request: httpx.Response(
                200,
                content=b"%PDF-1.4\nslow stream",
                request=request,
            )
        )

        with patch("app.source_retrieval.time.monotonic", side_effect=[0.0, 0.0, 6.0]):
            with self.assertRaisesRegex(ValueError, "total time limit"):
                retriever.retrieve_pdf("https://example.com/file.pdf")


class SourceRetrievalAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = SimpleNamespace(
            openai_api_key="test-key",
            openai_standard_model="gpt-5.6-luna",
            openai_maximum_model="gpt-5.6-terra",
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
        self.agent = BusinessKnowledgeAgent(settings, Mock(), Mock())

    def test_retrieved_pdf_is_a_chat_attachment_not_a_library_ingestion(self) -> None:
        original = b"%PDF-1.4\nsource\n%%EOF"
        self.agent._source_retriever = Mock()
        self.agent._source_retriever.retrieve_pdf.return_value = RetrievedSourceDocument(
            filename="product-datasheet.pdf",
            mime_type="application/pdf",
            content_bytes=original,
            source_url="https://manufacturer.example/product-datasheet.pdf",
        )

        payload, citations, summary, result = self.agent._execute_tool(
            "retrieve_source_pdf",
            {
                "url": "https://manufacturer.example/product-datasheet.pdf",
                "filename": "product-datasheet.pdf",
                "title": "Product datasheet",
            },
            "broader",
            RetrievalContext(),
        )

        self.assertTrue(payload["retrieved"])
        self.assertEqual(result.content_bytes, original)
        self.assertEqual(citations[0].source_url, payload["source_url"])
        self.assertIn("without adding it to the library", result.message)
        self.assertIn("Retrieved original source PDF", summary)
        self.agent._document_store.ingest.assert_not_called()

    def test_source_retrieval_is_not_available_in_internal_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "Context: Global"):
            self.agent._execute_tool(
                "retrieve_source_pdf",
                {"url": "https://example.com/file.pdf", "filename": None, "title": None},
                "internal",
                RetrievalContext(),
            )


if __name__ == "__main__":
    unittest.main()
