from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.ingestion import DocumentIngestionService, PDF_UPLOAD_SUFFIXES
from app.watch_folders import BINARY_WATCH_SUFFIXES


def build_pdf_pages(
    page_texts: list[str | None],
    password: str | None = None,
) -> bytes:
    writer = PdfWriter()
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        if text:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            resources = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): writer._add_object(font)}
                    )
                }
            )
            stream = DecodedStreamObject()
            escaped_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped_text}) Tj ET".encode("latin-1"))
            page[NameObject("/Resources")] = resources
            page[NameObject("/Contents")] = writer._add_object(stream)

    if password:
        writer.encrypt(password)

    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def build_pdf(text: str | None = None, password: str | None = None) -> bytes:
    return build_pdf_pages([text], password=password)


class PdfIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = object.__new__(DocumentIngestionService)
        self.service._settings = SimpleNamespace(
            pdf_ocr_enabled=True,
            pdf_ocr_engine="tesseract",
            pdf_ocr_language="eng",
            pdf_ocr_dpi=300,
            pdf_ocr_min_native_text_chars=10,
            pdf_ocr_timeout_seconds=60,
            pdf_ocr_tesseract_cmd="tesseract",
            pdf_max_pages=500,
        )

    def test_pdf_is_supported_as_binary_content(self) -> None:
        self.assertIn(".pdf", PDF_UPLOAD_SUFFIXES)
        self.assertIn(".pdf", BINARY_WATCH_SUFFIXES)

    def test_extracts_searchable_text_and_page_number(self) -> None:
        with patch.object(self.service, "_ocr_pdf_pages") as ocr_pages:
            extracted = self.service._extract_pdf_document_text(
                filename="field-report.pdf",
                content_bytes=build_pdf("Field report complete"),
            )

        self.assertEqual(extracted, "Page 1\nField report complete")
        ocr_pages.assert_not_called()

    def test_uses_ocr_for_image_only_pdf(self) -> None:
        with patch.object(
            self.service,
            "_ocr_pdf_pages",
            return_value={1: "Scanned field report"},
        ) as ocr_pages:
            extracted = self.service._extract_pdf_document_text(
                filename="scan.pdf",
                content_bytes=build_pdf(),
            )

        self.assertEqual(extracted, "Page 1\nScanned field report")
        ocr_pages.assert_called_once()
        self.assertEqual(ocr_pages.call_args.kwargs["page_numbers"], [1])

    def test_only_ocrs_low_text_pages_in_mixed_pdf(self) -> None:
        with patch.object(
            self.service,
            "_ocr_pdf_pages",
            return_value={2: "Scanned appendix"},
        ) as ocr_pages:
            extracted = self.service._extract_pdf_document_text(
                filename="mixed.pdf",
                content_bytes=build_pdf_pages(["Native field report", None]),
            )

        self.assertEqual(
            extracted,
            "Page 1\nNative field report\n\nPage 2\nScanned appendix",
        )
        self.assertEqual(ocr_pages.call_args.kwargs["page_numbers"], [2])

    def test_rejects_image_only_pdf_when_ocr_is_disabled(self) -> None:
        self.service._settings.pdf_ocr_enabled = False
        with self.assertRaisesRegex(ValueError, "Image-only PDFs require OCR"):
            self.service._extract_pdf_document_text(
                filename="scan.pdf",
                content_bytes=build_pdf(),
            )

    def test_rejects_image_only_pdf_when_ocr_finds_no_text(self) -> None:
        with patch.object(self.service, "_ocr_pdf_pages", return_value={1: ""}):
            with self.assertRaisesRegex(ValueError, "OCR ran but did not recognize"):
                self.service._extract_pdf_document_text(
                    filename="scan.pdf",
                    content_bytes=build_pdf(),
                )

    def test_rejects_password_protected_pdf(self) -> None:
        with self.assertRaisesRegex(ValueError, "password-protected"):
            self.service._extract_pdf_document_text(
                filename="locked.pdf",
                content_bytes=build_pdf("Confidential", password="secret"),
            )

    def test_rejects_invalid_pdf(self) -> None:
        with self.assertRaisesRegex(ValueError, "not a valid PDF"):
            self.service._extract_pdf_document_text(
                filename="broken.pdf",
                content_bytes=b"not a pdf",
            )

    def test_pdf_requires_binary_content_even_if_text_is_supplied(self) -> None:
        with self.assertRaisesRegex(ValueError, "did not include binary content"):
            self.service._extract_upload_text(
                filename="scan.pdf",
                suffix=".pdf",
                content_text="untrusted replacement text",
                content_base64=None,
            )

    def test_direct_pdf_upload_persists_extracted_text(self) -> None:
        with TemporaryDirectory() as temp_directory:
            temp_root = Path(temp_directory)
            settings = SimpleNamespace(
                docstore_backend="json",
                docstore_json_path=temp_root / "documents.json",
                docstore_folders_path=temp_root / "folders.json",
                openai_api_key=None,
                pdf_ocr_enabled=True,
                pdf_ocr_engine="tesseract",
                pdf_ocr_language="eng",
                pdf_ocr_dpi=300,
                pdf_ocr_min_native_text_chars=10,
                pdf_ocr_timeout_seconds=60,
                pdf_ocr_tesseract_cmd="tesseract",
                pdf_max_pages=500,
            )
            settings.docstore_json_path.write_text("[]\n", encoding="utf-8")
            settings.docstore_folders_path.write_text("[]\n", encoding="utf-8")
            service = DocumentIngestionService(settings)

            outcome = service.ingest_upload(
                filename="field-report.pdf",
                content_base64=base64.b64encode(
                    build_pdf("Field report direct upload")
                ).decode("ascii"),
            )

            self.assertEqual(outcome.created_count, 1)
            self.assertIn("Field report direct upload", outcome.uploaded_documents[0].text)

    def test_tolerant_batch_imports_valid_file_and_reports_invalid_pdf(self) -> None:
        with TemporaryDirectory() as temp_directory:
            temp_root = Path(temp_directory)
            settings = SimpleNamespace(
                docstore_backend="json",
                docstore_json_path=temp_root / "documents.json",
                docstore_folders_path=temp_root / "folders.json",
                openai_api_key=None,
                pdf_ocr_enabled=True,
                pdf_ocr_min_native_text_chars=10,
                pdf_max_pages=500,
            )
            settings.docstore_json_path.write_text("[]\n", encoding="utf-8")
            settings.docstore_folders_path.write_text("[]\n", encoding="utf-8")
            service = DocumentIngestionService(settings)

            outcome = service.ingest_upload_batch(
                uploads=[
                    {"filename": "good.txt", "content_text": "Readable note"},
                    {
                        "filename": "broken.pdf",
                        "content_base64": base64.b64encode(b"not a pdf").decode("ascii"),
                    },
                ],
                continue_on_error=True,
            )

            self.assertEqual(outcome.created_count, 1)
            self.assertEqual(
                [document.title for document in outcome.uploaded_documents],
                ["good.txt"],
            )
            self.assertEqual(len(outcome.failed_uploads), 1)
            self.assertIn("broken.pdf", outcome.failed_uploads[0])

    def test_rejects_pdf_over_configured_page_limit(self) -> None:
        self.service._settings.pdf_max_pages = 1
        with self.assertRaisesRegex(ValueError, "configured limit is 1 page"):
            self.service._extract_pdf_document_text(
                filename="too-long.pdf",
                content_bytes=build_pdf_pages(["Page one", "Page two"]),
            )

    def test_renders_page_and_hands_image_to_provider(self) -> None:
        provider = Mock()

        def recognize(image, **kwargs):
            self.assertEqual(kwargs["filename"], "scan.pdf")
            self.assertEqual(kwargs["page_number"], 1)
            self.assertGreater(image.width, 0)
            self.assertGreater(image.height, 0)
            self.assertEqual(image.info["dpi"], (300, 300))
            return "Rendered scan text"

        provider.recognize.side_effect = recognize

        with patch.object(self.service, "_get_ocr_provider", return_value=provider):
            extracted = self.service._ocr_pdf_pages(
                filename="scan.pdf",
                content_bytes=build_pdf(),
                page_numbers=[1],
            )

        self.assertEqual(extracted, {1: "Rendered scan text"})


if __name__ == "__main__":
    unittest.main()
