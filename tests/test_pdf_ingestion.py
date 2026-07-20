from __future__ import annotations

from io import BytesIO
import unittest

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.ingestion import DocumentIngestionService, PDF_UPLOAD_SUFFIXES
from app.watch_folders import BINARY_WATCH_SUFFIXES


def build_pdf(text: str | None = None, password: str | None = None) -> bytes:
    writer = PdfWriter()
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


class PdfIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = object.__new__(DocumentIngestionService)

    def test_pdf_is_supported_as_binary_content(self) -> None:
        self.assertIn(".pdf", PDF_UPLOAD_SUFFIXES)
        self.assertIn(".pdf", BINARY_WATCH_SUFFIXES)

    def test_extracts_searchable_text_and_page_number(self) -> None:
        extracted = self.service._extract_pdf_document_text(
            filename="field-report.pdf",
            content_bytes=build_pdf("Field report complete"),
        )

        self.assertEqual(extracted, "Page 1\nField report complete")

    def test_rejects_image_only_pdf_without_ocr(self) -> None:
        with self.assertRaisesRegex(ValueError, "Image-only PDFs require OCR"):
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


if __name__ == "__main__":
    unittest.main()
