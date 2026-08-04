"""Verify generated PDF layout retains headings, tables, sources, and pagination."""

from __future__ import annotations

import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from pypdf import PdfReader

from app.datastore import DocumentRecord
from app.document_generator import ContextDocumentGenerator, PDF_MIME_TYPE
from app.models import Citation, ContextFilter, DocumentGenerationRequest
from app.openai_agent import TOOLS


class PdfDocumentGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.generator = ContextDocumentGenerator(SimpleNamespace(), Mock())

    def test_pdf_is_a_supported_document_generation_format(self) -> None:
        request = DocumentGenerationRequest(
            instructions="Create a project summary.",
            output_format="pdf",
        )

        self.assertEqual(request.output_format, "pdf")
        document_tool = next(tool for tool in TOOLS if tool.get("name") == "generate_context_document")
        self.assertIn("pdf", document_tool["parameters"]["properties"]["output_format"]["enum"])
        self.assertEqual(PDF_MIME_TYPE, "application/pdf")

    def test_build_pdf_bytes_creates_readable_paginated_document(self) -> None:
        content = (
            "Executive Summary\n"
            "This document summarizes the internal project material.\n\n"
            "Key Items:\n"
            "- First requirement\n"
            "- Second requirement\n\n"
            "| Owner | Status |\n"
            "|---|---|\n"
            "| Engineering | Active |\n\n"
            "Sources\n"
            "- [DOC-1] Project Notes (project)\n"
        )

        pdf_bytes = self.generator._build_pdf_bytes(
            title="Project Delivery Summary",
            content=content,
        )

        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        reader = PdfReader(io.BytesIO(pdf_bytes))
        self.assertGreaterEqual(len(reader.pages), 2)
        extracted_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertIn("Project Delivery Summary", extracted_text)
        self.assertIn("First requirement", extracted_text)
        self.assertIn("Engineering", extracted_text)
        self.assertIn("Sources", extracted_text)
        self.assertIn("Page 1", extracted_text)

    def test_generate_document_returns_pdf_download_payload(self) -> None:
        citation = Citation(
            document_id="DOC-1",
            title="Project Notes",
            category="project",
        )
        self.generator._collect_supporting_documents = Mock(
            return_value=([Mock()], [citation])
        )
        self.generator._generate_text_document = Mock(
            return_value="Summary\nGenerated from the selected project notes."
        )

        result = self.generator.generate_document(
            instructions="Create a polished project summary.",
            title="Project Summary",
            output_format="pdf",
            source_mode="internal",
            reasoning_mode="standard",
            context_filter=ContextFilter(),
        )

        self.assertEqual(result.filename, "project-summary.pdf")
        self.assertEqual(result.mime_type, "application/pdf")
        self.assertTrue(result.content_bytes.startswith(b"%PDF-"))
        self.assertEqual(result.citations, [citation])

    def test_generate_document_reuses_chat_selected_documents_without_searching_again(self) -> None:
        document_store = Mock()
        document_store.get_document.return_value = DocumentRecord(
            document_id="DOC-1",
            title="Project Notes",
            category="project",
            folder="Projects/Test",
            tags=[],
            summary="Selected by the chat.",
            text="Supporting text.",
        )
        generator = ContextDocumentGenerator(SimpleNamespace(), document_store)
        generator._collect_supporting_documents = Mock()
        generator._generate_text_document = Mock(return_value="Generated summary.")

        result = generator.generate_document(
            instructions="Create a project summary.",
            title="Project Summary",
            output_format="pdf",
            source_mode="internal",
            reasoning_mode="standard",
            context_filter=ContextFilter(),
            supporting_document_ids=["DOC-1", "DOC-1"],
        )

        self.assertEqual(result.filename, "project-summary.pdf")
        document_store.get_document.assert_called_once()
        generator._collect_supporting_documents.assert_not_called()


if __name__ == "__main__":
    unittest.main()
