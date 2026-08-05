"""Verify streamed deletion emits parseable progress and a terminal result."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from app.ingestion import DeleteOutcome
from app.models import DocumentDeleteRequest
from tests.main_runtime import main


delete_documents_stream = main.delete_documents_stream


class DocumentDeleteProgressTests(unittest.TestCase):
    def test_stream_reports_progress_before_result(self) -> None:
        def delete_side_effect(*, document_ids, progress_callback):
            self.assertEqual(document_ids, ["DOC-1"])
            progress_callback("validating", 5, "Validating selected documents...")
            progress_callback("committing", 92, "Committing database changes...")
            return DeleteOutcome(
                deleted_document_ids=["DOC-1"],
                semantic_index_rebuilt=False,
                message="Deleted 1 document and updated the semantic index.",
            )

        async def read_body(response) -> str:
            chunks: list[str] = []
            async for chunk in response.body_iterator:
                chunks.append(
                    chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
                )
            return "".join(chunks)

        with (
            patch("app.main.ingestion_service.delete_documents", side_effect=delete_side_effect),
            patch("app.main._invalidate_document_store_cache"),
        ):
            response = delete_documents_stream(
                DocumentDeleteRequest(document_ids=["DOC-1"])
            )
            body = asyncio.run(read_body(response))

        events = [json.loads(line) for line in body.splitlines() if line.strip()]
        self.assertEqual(
            [event["type"] for event in events],
            ["progress", "progress", "result"],
        )
        self.assertEqual(events[0]["percent"], 5)
        self.assertEqual(events[1]["percent"], 92)
        self.assertEqual(events[-1]["payload"]["total_deleted"], 1)


if __name__ == "__main__":
    unittest.main()
