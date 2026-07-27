from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.datastore import RetrievalContext
from app.document_generator import ContextDocumentGenerator
from app.models import Citation, ToolTrace
from app.openai_agent import BusinessKnowledgeAgent


class BroaderModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            openai_api_key="test-key",
            openai_standard_model="gpt-5.6-luna",
            openai_maximum_model="gpt-5.6-terra",
            openai_standard_reasoning_effort="medium",
            openai_maximum_reasoning_effort="max",
            openai_text_verbosity="medium",
            openai_store_responses=False,
        )
        self.agent = BusinessKnowledgeAgent(self.settings, Mock(), Mock())
        self.agent._client = Mock()
        self.agent._client.responses.create.return_value = SimpleNamespace(output=[])

    def test_web_search_tool_is_available_only_in_broader_mode(self) -> None:
        context = RetrievalContext()

        self.agent._run_response([], "internal", context)
        internal_request = self.agent._client.responses.create.call_args.kwargs
        self.assertNotIn("web_search", [tool["type"] for tool in internal_request["tools"]])

        self.agent._run_response([], "broader", context)
        broader_request = self.agent._client.responses.create.call_args.kwargs
        self.assertIn("web_search", [tool["type"] for tool in broader_request["tools"]])

    def test_reasoning_mode_selects_the_validated_model_profile(self) -> None:
        context = RetrievalContext()

        self.agent._run_response([], "internal", context, "standard")
        standard_request = self.agent._client.responses.create.call_args.kwargs
        self.assertEqual(standard_request["model"], "gpt-5.6-luna")
        self.assertEqual(standard_request["reasoning"], {"effort": "medium"})

        self.agent._run_response([], "internal", context, "maximum")
        maximum_request = self.agent._client.responses.create.call_args.kwargs
        self.assertEqual(maximum_request["model"], "gpt-5.6-terra")
        self.assertEqual(maximum_request["reasoning"], {"effort": "max"})

    def test_document_generation_uses_the_selected_reasoning_profile(self) -> None:
        generator = ContextDocumentGenerator(self.settings, Mock())
        context = RetrievalContext()

        request = generator._build_model_request(
            source_mode="internal",
            reasoning_mode="standard",
            retrieval_context=context,
            extra_instructions="Create a concise test document.",
            user_payload={"instructions": "Test"},
        )
        self.assertEqual(request["model"], "gpt-5.6-luna")
        self.assertEqual(request["reasoning"], {"effort": "medium"})

        request = generator._build_model_request(
            source_mode="internal",
            reasoning_mode="maximum",
            retrieval_context=context,
            extra_instructions="Create a concise test document.",
            user_payload={"instructions": "Test"},
        )
        self.assertEqual(request["model"], "gpt-5.6-terra")
        self.assertEqual(request["reasoning"], {"effort": "max"})

    def test_collects_web_search_trace_and_url_citation(self) -> None:
        output_items = [
            SimpleNamespace(
                type="web_search_call",
                action=SimpleNamespace(type="search", query="current VMSS guidance"),
            ),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        annotations=[
                            SimpleNamespace(
                                type="url_citation",
                                url="https://example.com/vmss",
                                title="Current VMSS guidance",
                            )
                        ]
                    )
                ],
            ),
        ]

        citations, traces = self.agent._collect_web_search_metadata(output_items)

        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0].category, "web")
        self.assertEqual(citations[0].source_url, "https://example.com/vmss")
        self.assertTrue(citations[0].document_id.startswith("WEB-"))
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].tool_name, "web_search")
        self.assertEqual(traces[0].arguments["query"], "current VMSS guidance")

    def test_web_citations_are_kept_with_explicit_internal_citations(self) -> None:
        internal_citation = Citation(
            document_id="DOC-1",
            title="Internal note",
            category="project",
        )
        web_citation = Citation(
            document_id="WEB-ABC",
            title="Public source",
            category="web",
            source_url="https://example.com/source",
        )
        citations = OrderedDict(
            ((internal_citation.document_id, internal_citation), (web_citation.document_id, web_citation))
        )

        selected = self.agent._select_final_citations(
            assistant_message="Internal fact [DOC-1] with current public context.",
            citations=citations,
            traces=[ToolTrace(tool_name="web_search", arguments={}, summary="Searched the web.")],
        )

        self.assertEqual([citation.document_id for citation in selected], ["DOC-1", "WEB-ABC"])


if __name__ == "__main__":
    unittest.main()
