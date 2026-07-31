from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import logging
import re
from typing import Any

from openai import OpenAI

from app.config import Settings


logger = logging.getLogger("app.pdf_ingestion")


@dataclass(frozen=True)
class PdfVisionPage:
    page_number: int
    image_bytes: bytes
    native_text: str = ""
    ocr_text: str = ""


class PdfVisionAnalyzer:
    def __init__(
        self,
        settings: Settings,
        *,
        client: OpenAI | None = None,
    ) -> None:
        self._settings = settings
        self._client = client

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            if not self._settings.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY is required for PDF image interpretation.")
            self._client = OpenAI(
                api_key=self._settings.openai_api_key,
                timeout=max(1, int(self._settings.pdf_vision_timeout_seconds)),
                max_retries=1,
            )
        return self._client

    def analyze_pages(self, pages: list[PdfVisionPage]) -> dict[int, str]:
        if not pages:
            return {}

        batch_size = max(1, min(6, int(self._settings.pdf_vision_batch_size)))
        results: dict[int, str] = {}
        for start_index in range(0, len(pages), batch_size):
            batch = pages[start_index : start_index + batch_size]
            results.update(self._analyze_batch(batch))
        return results

    def _analyze_batch(self, pages: list[PdfVisionPage]) -> dict[int, str]:
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": (
                    "Interpret the visual information on these PDF pages for a searchable internal "
                    "document library. Describe only information visibly supported by the page. "
                    "Capture chart values and trends, diagram components and connections, table or "
                    "drawing labels, legends, callouts, equipment or product details, photographs, "
                    "and spatial relationships that OCR alone would miss. Do not repeat ordinary "
                    "body text unless it is necessary to explain a visual. Return only JSON with "
                    'this shape: {"pages":[{"page_number":1,"visual_summary":"..."}]}. '
                    "Use an empty visual_summary when there is no meaningful visual information."
                ),
            }
        ]
        for page in pages:
            context_parts = [f"PDF page {page.page_number}."]
            if page.native_text.strip():
                context_parts.append(
                    "Native page text for context:\n"
                    f"{page.native_text.strip()[:4000]}"
                )
            if page.ocr_text.strip():
                context_parts.append(
                    "OCR text for context:\n"
                    f"{page.ocr_text.strip()[:4000]}"
                )
            content.append({"type": "input_text", "text": "\n\n".join(context_parts)})
            content.append(
                {
                    "type": "input_image",
                    "image_url": (
                        "data:image/jpeg;base64,"
                        + base64.b64encode(page.image_bytes).decode("ascii")
                    ),
                    "detail": "high",
                }
            )

        response = self.client.responses.create(
            model=self._settings.pdf_vision_model,
            input=[{"role": "user", "content": content}],
            reasoning={"effort": "low"},
            text={"verbosity": "low"},
            store=self._settings.openai_store_responses,
        )
        return self._parse_response(
            getattr(response, "output_text", ""),
            expected_page_numbers={page.page_number for page in pages},
        )

    @staticmethod
    def _parse_response(
        raw_output: str,
        *,
        expected_page_numbers: set[int],
    ) -> dict[int, str]:
        normalized = str(raw_output or "").strip()
        if normalized.startswith("```"):
            normalized = re.sub(
                r"^```(?:json)?\s*|\s*```$",
                "",
                normalized,
                flags=re.IGNORECASE | re.DOTALL,
            ).strip()
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError as exc:
            raise RuntimeError("PDF vision returned an invalid response.") from exc

        raw_pages = payload.get("pages") if isinstance(payload, dict) else None
        if not isinstance(raw_pages, list):
            raise RuntimeError("PDF vision did not return page results.")

        results: dict[int, str] = {}
        for item in raw_pages:
            if not isinstance(item, dict):
                continue
            try:
                page_number = int(item.get("page_number"))
            except (TypeError, ValueError):
                continue
            if page_number not in expected_page_numbers:
                continue
            summary = str(item.get("visual_summary") or "").strip()
            if summary:
                results[page_number] = summary
        return results


def get_pdf_vision_runtime_status(settings: Settings) -> dict[str, object]:
    enabled = bool(getattr(settings, "pdf_vision_enabled", True))
    configured = bool(getattr(settings, "openai_api_key", None))
    return {
        "enabled": enabled,
        "available": enabled and configured,
        "model": getattr(settings, "pdf_vision_model", "gpt-5.6-luna"),
        "max_pages": max(0, int(getattr(settings, "pdf_vision_max_pages", 12))),
        "detail": (
            "PDF image interpretation is enabled."
            if enabled and configured
            else "OPENAI_API_KEY is required for PDF image interpretation."
            if enabled
            else "PDF image interpretation is disabled."
        ),
    }
