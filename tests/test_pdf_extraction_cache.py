from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.ingestion import DocumentIngestionService


class PdfExtractionCacheTests(unittest.TestCase):
    def _settings(self, root: Path, *, vision_model: str = "vision-model-a") -> SimpleNamespace:
        return SimpleNamespace(
            docstore_json_path=root / "documents.json",
            pdf_extraction_cache_enabled=True,
            pdf_extraction_cache_path=root / "pdf-cache",
            pdf_max_pages=500,
            pdf_ocr_enabled=True,
            pdf_ocr_engine="rapidocr",
            pdf_ocr_language="eng",
            pdf_ocr_dpi=300,
            pdf_ocr_min_native_text_chars=40,
            pdf_image_ocr_enabled=True,
            pdf_image_ocr_max_pages=100,
            pdf_vision_enabled=True,
            pdf_vision_model=vision_model,
            pdf_vision_max_pages=12,
            pdf_vision_batch_size=3,
            pdf_vision_dpi=144,
            pdf_vision_max_dimension=1800,
        )

    def test_identical_pdf_bytes_reuse_extracted_text_without_reprocessing(self) -> None:
        with TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            service = DocumentIngestionService(self._settings(root))

            with patch.object(
                service,
                "_extract_pdf_document_text_impl",
                return_value="Page 1\nCached searchable text",
            ) as extract:
                first = service._extract_pdf_document_text(
                    filename="first-name.pdf",
                    content_bytes=b"same-pdf-bytes",
                )
                second = service._extract_pdf_document_text(
                    filename="renamed.pdf",
                    content_bytes=b"same-pdf-bytes",
                )

            self.assertEqual(first, second)
            extract.assert_called_once()
            self.assertEqual(len(list((root / "pdf-cache").glob("*.json"))), 1)

    def test_cache_key_changes_with_pdf_bytes_or_extraction_settings(self) -> None:
        with TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            first_service = DocumentIngestionService(self._settings(root))
            second_service = DocumentIngestionService(
                self._settings(root, vision_model="vision-model-b")
            )

            with patch.object(
                first_service,
                "_extract_pdf_document_text_impl",
                side_effect=["first", "second"],
            ) as first_extract:
                first_service._extract_pdf_document_text(
                    filename="document.pdf",
                    content_bytes=b"pdf-version-one",
                )
                first_service._extract_pdf_document_text(
                    filename="document.pdf",
                    content_bytes=b"pdf-version-two",
                )

            with patch.object(
                second_service,
                "_extract_pdf_document_text_impl",
                return_value="third",
            ) as second_extract:
                second_service._extract_pdf_document_text(
                    filename="document.pdf",
                    content_bytes=b"pdf-version-one",
                )

            self.assertEqual(first_extract.call_count, 2)
            second_extract.assert_called_once()
            self.assertEqual(len(list((root / "pdf-cache").glob("*.json"))), 3)


if __name__ == "__main__":
    unittest.main()
