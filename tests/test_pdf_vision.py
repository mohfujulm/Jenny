"""Test page batching and structured response parsing for PDF vision analysis."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.pdf_vision import PdfVisionAnalyzer, PdfVisionPage


class PdfVisionAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            openai_api_key="test-key",
            openai_store_responses=False,
            pdf_vision_model="gpt-5.6-luna",
            pdf_vision_batch_size=3,
            pdf_vision_timeout_seconds=60,
        )
        self.client = Mock()
        self.analyzer = PdfVisionAnalyzer(self.settings, client=self.client)

    def test_analyzes_images_with_page_context(self) -> None:
        self.client.responses.create.return_value = SimpleNamespace(
            output_text=(
                '{"pages":[{"page_number":2,'
                '"visual_summary":"Chart rises from 10 to 20 units."}]}'
            )
        )

        results = self.analyzer.analyze_pages(
            [
                PdfVisionPage(
                    page_number=2,
                    image_bytes=b"jpeg-image",
                    native_text="Quarterly output",
                    ocr_text="Q1 10 Q2 20",
                )
            ]
        )

        self.assertEqual(results, {2: "Chart rises from 10 to 20 units."})
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "gpt-5.6-luna")
        self.assertEqual(request["reasoning"], {"effort": "low"})
        content = request["input"][0]["content"]
        image_item = next(item for item in content if item["type"] == "input_image")
        self.assertTrue(image_item["image_url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(image_item["detail"], "high")

    def test_rejects_unstructured_model_output(self) -> None:
        self.client.responses.create.return_value = SimpleNamespace(
            output_text="The page contains a diagram."
        )

        with self.assertRaisesRegex(RuntimeError, "invalid response"):
            self.analyzer.analyze_pages(
                [PdfVisionPage(page_number=1, image_bytes=b"jpeg-image")]
            )


if __name__ == "__main__":
    unittest.main()
