from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from app.ocr import (
    RapidOcrProvider,
    TesseractOcrProvider,
    create_ocr_provider,
    get_ocr_runtime_status,
)


class OcrProviderTests(unittest.TestCase):
    def test_tesseract_provider_streams_png_and_returns_text(self) -> None:
        provider = TesseractOcrProvider(
            command="tesseract-test",
            language="eng+spa",
            timeout_seconds=15,
        )
        image = Image.new("RGB", (80, 40), "white")
        image.info["dpi"] = (240, 240)

        def fake_run(command, **kwargs):
            self.assertEqual(command[1:3], ["stdin", "stdout"])
            self.assertIn("eng+spa", command)
            self.assertIn("240", command)
            self.assertTrue(kwargs["input"].startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(kwargs["timeout"], 15)
            return subprocess.CompletedProcess(command, 0, b"Invoice total\n", b"")

        with (
            patch("app.ocr.resolve_tesseract_command", return_value="tesseract-test"),
            patch("app.ocr.subprocess.run", side_effect=fake_run),
        ):
            text = provider.recognize(
                image,
                filename="invoice.pdf",
                page_number=2,
            )

        self.assertEqual(text, "Invoice total")

    def test_rapidocr_provider_joins_detected_lines(self) -> None:
        calls = []

        def engine(image_array):
            calls.append(image_array.shape)
            return SimpleNamespace(txts=("First line", "Second line"))

        provider = RapidOcrProvider(engine_factory=lambda: engine)
        fake_array = SimpleNamespace(shape=(50, 100, 3))
        fake_numpy = SimpleNamespace(asarray=lambda image: fake_array)
        with patch.dict(sys.modules, {"numpy": fake_numpy}):
            text = provider.recognize(
                Image.new("RGB", (100, 50), "white"),
                filename="scan.pdf",
                page_number=1,
            )

        self.assertEqual(text, "First line\nSecond line")
        self.assertEqual(calls, [(50, 100, 3)])

    def test_factory_selects_configured_provider(self) -> None:
        settings = SimpleNamespace(
            pdf_ocr_engine="rapidocr",
            pdf_ocr_tesseract_cmd="tesseract",
            pdf_ocr_language="eng",
            pdf_ocr_timeout_seconds=60,
        )
        self.assertIsInstance(create_ocr_provider(settings), RapidOcrProvider)

    def test_runtime_status_reports_missing_rapidocr_dependencies(self) -> None:
        settings = SimpleNamespace(pdf_ocr_enabled=True, pdf_ocr_engine="rapidocr")
        with patch("app.ocr.importlib.util.find_spec", return_value=None):
            status = get_ocr_runtime_status(settings)

        self.assertFalse(status["available"])
        self.assertEqual(status["engine"], "rapidocr")


if __name__ == "__main__":
    unittest.main()
