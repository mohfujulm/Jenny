"""Generate downloadable TXT, DOCX, PDF, and XLSX files from library context.

The service first retrieves supporting documents, asks the model for structured
content, appends traceable sources, and renders the requested binary format in
memory.  The low-level XML builders intentionally avoid requiring desktop Office
software on the server.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import io
import json
import re
from typing import Any
from xml.sax.saxutils import escape as xml_escape
import zipfile

from openai import OpenAI
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.config import Settings
from app.datastore import BaseDocumentStore, DocumentRecord, RetrievalContext
from app.models import Citation, ContextFilter, GeneratedDocumentFormat, ReasoningMode, SourceMode
from app.openai_usage import record_openai_usage, response_has_usage
from app.prompts import build_context_scope_prompt, build_system_prompt
from app.reasoning_profiles import get_chat_reasoning_profile


TEXT_MIME_TYPE = "text/plain; charset=utf-8"
DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MIME_TYPE = "application/pdf"
SOURCE_SHEET_NAME = "Sources"
DEFAULT_TEXT_DOCUMENT_NAME = "generated-document"
DEFAULT_WORKBOOK_NAME = "generated-workbook"
MAX_SUPPORTING_DOCUMENTS = 3
MAX_SUPPORTING_DOCUMENT_CHARS = 4_000
SPREADSHEET_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_DOCUMENT_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


@dataclass(frozen=True)
class GeneratedDocumentResult:
    """Generated file bytes plus download metadata and source citations."""
    filename: str
    mime_type: str
    content_bytes: bytes
    message: str
    citations: list[Citation]


class ContextDocumentGenerator:
    """Retrieve evidence, draft content, and serialize it to the requested format."""
    def __init__(self, settings: Settings, document_store: BaseDocumentStore) -> None:
        self._settings = settings
        self._document_store = document_store
        self._client: OpenAI | None = None

    @property
    def client(self) -> OpenAI:
        if not self._settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured.")
        if self._client is None:
            self._client = OpenAI(
                api_key=self._settings.openai_api_key,
                timeout=max(
                    1,
                    int(getattr(self._settings, "openai_request_timeout_seconds", 60)),
                ),
                max_retries=0,
            )
        return self._client

    def generate_document(
        self,
        *,
        instructions: str,
        title: str | None,
        output_format: GeneratedDocumentFormat,
        source_mode: SourceMode,
        reasoning_mode: ReasoningMode = "standard",
        context_filter: ContextFilter,
        supporting_document_ids: list[str] | None = None,
        client: OpenAI | None = None,
        timeout_seconds: float | None = None,
    ) -> GeneratedDocumentResult:
        normalized_instructions = str(instructions or "").strip()
        if not normalized_instructions:
            raise ValueError("Document instructions are required.")

        normalized_title = str(title or "").strip() or self._derive_title_from_instructions(normalized_instructions)
        retrieval_context = RetrievalContext.from_lists(
            folder_ids=context_filter.folder_ids,
            document_ids=context_filter.document_ids,
        )
        if source_mode == "broader" and not retrieval_context.is_active:
            raise ValueError(
                "Select Context: Internal or choose a library scope before generating from internal documents."
            )
        supporting_documents: list[DocumentRecord] = []
        citations: list[Citation] = []
        if supporting_document_ids:
            supporting_documents, citations = self._load_supporting_documents(
                document_ids=supporting_document_ids,
                retrieval_context=retrieval_context,
            )
        if not supporting_documents:
            supporting_documents, citations = self._collect_supporting_documents(
                query=f"{normalized_title}\n{normalized_instructions}".strip(),
                retrieval_context=retrieval_context,
            )
        if not supporting_documents:
            raise ValueError(
                "No relevant indexed documents were found in the current library scope for this request."
            )

        if output_format == "xlsx":
            workbook_bytes = self._generate_workbook_bytes(
                title=normalized_title,
                instructions=normalized_instructions,
                source_mode=source_mode,
                reasoning_mode=reasoning_mode,
                retrieval_context=retrieval_context,
                supporting_documents=supporting_documents,
                citations=citations,
                client=client,
                timeout_seconds=timeout_seconds,
            )
            filename = self._build_download_filename(normalized_title, output_format)
            return GeneratedDocumentResult(
                filename=filename,
                mime_type=XLSX_MIME_TYPE,
                content_bytes=workbook_bytes,
                message=self._build_success_message(filename, citations),
                citations=citations,
            )

        document_text = self._generate_text_document(
            title=normalized_title,
            instructions=normalized_instructions,
            output_format=output_format,
            source_mode=source_mode,
            reasoning_mode=reasoning_mode,
            retrieval_context=retrieval_context,
            supporting_documents=supporting_documents,
            citations=citations,
            client=client,
            timeout_seconds=timeout_seconds,
        )
        filename = self._build_download_filename(normalized_title, output_format)
        if output_format == "docx":
            content_bytes = self._build_docx_bytes(title=normalized_title, content=document_text)
            mime_type = DOCX_MIME_TYPE
        elif output_format == "pdf":
            content_bytes = self._build_pdf_bytes(title=normalized_title, content=document_text)
            mime_type = PDF_MIME_TYPE
        else:
            content_bytes = document_text.encode("utf-8")
            mime_type = TEXT_MIME_TYPE

        return GeneratedDocumentResult(
            filename=filename,
            mime_type=mime_type,
            content_bytes=content_bytes,
            message=self._build_success_message(filename, citations),
            citations=citations,
        )

    def render_text_document(
        self,
        *,
        title: str,
        body: str,
        output_format: GeneratedDocumentFormat,
        citations: list[Citation] | None = None,
    ) -> GeneratedDocumentResult:
        """Package already-generated text without making another model call.

        Scheduled routines own a strict one-request execution budget.  Keeping
        deterministic rendering public lets them reuse the established PDF and
        DOCX serializers without accidentally invoking document generation a
        second time.
        """
        if output_format not in {"txt", "docx", "pdf"}:
            raise ValueError("Text documents can only be rendered as TXT, DOCX, or PDF.")
        normalized_title = str(title or "").strip() or DEFAULT_TEXT_DOCUMENT_NAME
        normalized_body = str(body or "").strip()
        if not normalized_body:
            raise ValueError("Document content is required.")
        source_citations = list(citations or [])
        document_text = self._append_sources_section(normalized_body, source_citations)
        filename = self._build_download_filename(normalized_title, output_format)
        if output_format == "docx":
            content_bytes = self._build_docx_bytes(
                title=normalized_title,
                content=document_text,
            )
            mime_type = DOCX_MIME_TYPE
        elif output_format == "pdf":
            content_bytes = self._build_pdf_bytes(
                title=normalized_title,
                content=document_text,
            )
            mime_type = PDF_MIME_TYPE
        else:
            content_bytes = document_text.encode("utf-8")
            mime_type = TEXT_MIME_TYPE
        return GeneratedDocumentResult(
            filename=filename,
            mime_type=mime_type,
            content_bytes=content_bytes,
            message=self._build_success_message(filename, source_citations),
            citations=source_citations,
        )

    def _collect_supporting_documents(
        self,
        *,
        query: str,
        retrieval_context: RetrievalContext,
    ) -> tuple[list[DocumentRecord], list[Citation]]:
        hits = self._document_store.search_documents(
            query=query,
            limit=MAX_SUPPORTING_DOCUMENTS,
            context=retrieval_context,
            search_profile="answer",
        )
        documents: list[DocumentRecord] = []
        citations: "OrderedDict[str, Citation]" = OrderedDict()
        seen_document_ids: set[str] = set()

        for hit in hits:
            citations.setdefault(
                hit.document_id,
                Citation(
                    document_id=hit.document_id,
                    title=hit.title,
                    category=hit.category,
                    excerpt=hit.excerpt,
                    source_url=hit.source_url,
                ),
            )
            document = self._document_store.get_document(hit.document_id, context=retrieval_context)
            if document is None or document.document_id in seen_document_ids:
                continue
            seen_document_ids.add(document.document_id)
            documents.append(document)
            if len(documents) >= MAX_SUPPORTING_DOCUMENTS:
                break

        if documents:
            return documents, list(citations.values())

        library = self._document_store.list_documents()
        for summary in library.documents:
            candidate = DocumentRecord(
                document_id=summary.document_id,
                title=summary.title,
                category=summary.category,
                folder=summary.folder,
                tags=summary.tags,
                summary=summary.summary,
                text="",
                source_url=summary.source_url,
                updated_at=summary.updated_at,
            )
            if not retrieval_context.allows_document(candidate):
                continue
            document = self._document_store.get_document(summary.document_id, context=retrieval_context)
            if document is None or document.document_id in seen_document_ids:
                continue
            seen_document_ids.add(document.document_id)
            documents.append(document)
            citations.setdefault(
                document.document_id,
                Citation(
                    document_id=document.document_id,
                    title=document.title,
                    category=document.category,
                    excerpt=document.summary,
                    source_url=document.source_url,
                ),
            )
            if len(documents) >= MAX_SUPPORTING_DOCUMENTS:
                break

        return documents, list(citations.values())

    def _load_supporting_documents(
        self,
        *,
        document_ids: list[str],
        retrieval_context: RetrievalContext,
    ) -> tuple[list[DocumentRecord], list[Citation]]:
        """Reuse documents the chat already selected without another embedding query."""
        documents: list[DocumentRecord] = []
        citations: list[Citation] = []
        seen_document_ids: set[str] = set()
        for item in document_ids:
            document_id = str(item or "").strip()
            if not document_id or document_id in seen_document_ids:
                continue
            seen_document_ids.add(document_id)
            document = self._document_store.get_document(
                document_id,
                context=retrieval_context,
            )
            if document is None:
                continue
            documents.append(document)
            citations.append(
                Citation(
                    document_id=document.document_id,
                    title=document.title,
                    category=document.category,
                    excerpt=document.summary,
                    source_url=document.source_url,
                )
            )
            if len(documents) >= MAX_SUPPORTING_DOCUMENTS:
                break
        return documents, citations

    def _generate_text_document(
        self,
        *,
        title: str,
        instructions: str,
        output_format: GeneratedDocumentFormat,
        source_mode: SourceMode,
        reasoning_mode: ReasoningMode,
        retrieval_context: RetrievalContext,
        supporting_documents: list[DocumentRecord],
        citations: list[Citation],
        client: OpenAI | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        context_payload = [
            document.to_tool_payload(max_chars=MAX_SUPPORTING_DOCUMENT_CHARS)
            for document in supporting_documents
        ]
        request = self._build_model_request(
            source_mode=source_mode,
            reasoning_mode=reasoning_mode,
            retrieval_context=retrieval_context,
            extra_instructions=(
                "You are generating a downloadable business document from internal library context.\n"
                "- Use the supplied internal documents as the primary factual basis.\n"
                "- Write the document body only. Do not add chat commentary, code fences, or JSON.\n"
                "- Use plain-text headings and bullets when useful.\n"
                "- If information is missing from the internal documents, say that briefly inside the document instead of inventing facts.\n"
                f"- Requested output format: {output_format}.\n"
                "- The final file will be packaged by the application, so focus on the content itself."
            ),
            user_payload={
                "requested_title": title,
                "instructions": instructions,
                "supporting_documents": context_payload,
            },
        )
        response = self._create_attributed_response(
            request,
            purpose=f"document_generation_{output_format}",
            item_count=len(supporting_documents),
            client=client,
            timeout_seconds=timeout_seconds,
        )
        generated_text = response.output_text.strip()
        if not generated_text:
            raise RuntimeError("The model returned an empty document.")

        return self._append_sources_section(generated_text, citations)

    def _generate_workbook_bytes(
        self,
        *,
        title: str,
        instructions: str,
        source_mode: SourceMode,
        reasoning_mode: ReasoningMode,
        retrieval_context: RetrievalContext,
        supporting_documents: list[DocumentRecord],
        citations: list[Citation],
        client: OpenAI | None = None,
        timeout_seconds: float | None = None,
    ) -> bytes:
        context_payload = [
            document.to_tool_payload(max_chars=MAX_SUPPORTING_DOCUMENT_CHARS)
            for document in supporting_documents
        ]
        request = self._build_model_request(
            source_mode=source_mode,
            reasoning_mode=reasoning_mode,
            retrieval_context=retrieval_context,
            extra_instructions=(
                "You are generating spreadsheet content from internal library context.\n"
                "- Use the supplied internal documents as the primary factual basis.\n"
                "- Return only a JSON object with this shape:\n"
                "  {\"workbook_title\": string, \"sheets\": [{\"name\": string, \"rows\": array}]}\n"
                "- Each sheet row must be either an array of cell values or an object with column keys.\n"
                "- Cell values must be strings, numbers, booleans, or empty strings.\n"
                "- Include header rows where appropriate.\n"
                "- Keep the workbook compact and practical for a small-business workflow.\n"
                "- Do not wrap the JSON in markdown fences."
            ),
            user_payload={
                "requested_title": title,
                "instructions": instructions,
                "supporting_documents": context_payload,
            },
        )
        response = self._create_attributed_response(
            request,
            purpose="document_generation_xlsx",
            item_count=len(supporting_documents),
            client=client,
            timeout_seconds=timeout_seconds,
        )
        workbook_payload = self._parse_workbook_payload(response.output_text)
        workbook_title, sheets = self._normalize_workbook_payload(workbook_payload, fallback_title=title)
        sheets = [*sheets, self._build_sources_sheet(citations)]
        return self._build_xlsx_bytes(workbook_title=workbook_title, sheets=sheets)

    def _build_model_request(
        self,
        *,
        source_mode: SourceMode,
        reasoning_mode: ReasoningMode,
        retrieval_context: RetrievalContext,
        extra_instructions: str,
        user_payload: dict[str, Any],
    ) -> dict[str, Any]:
        reasoning_profile = get_chat_reasoning_profile(self._settings, reasoning_mode)
        request: dict[str, Any] = {
            "model": reasoning_profile["model"],
            "input": [
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=True),
                }
            ],
            "instructions": (
                f"{build_system_prompt(source_mode)}\n\n"
                f"{build_context_scope_prompt(sorted(retrieval_context.folder_ids), sorted(retrieval_context.document_ids), source_mode)}\n\n"
                f"{extra_instructions}"
            ),
            "reasoning": {"effort": reasoning_profile["effort"]},
            "text": {"verbosity": self._settings.openai_text_verbosity},
            "max_output_tokens": max(
                512,
                int(getattr(self._settings, "document_generation_max_output_tokens", 6_000)),
            ),
            "store": self._settings.openai_store_responses,
        }
        if not self._settings.openai_store_responses:
            request["include"] = ["reasoning.encrypted_content"]
        return request

    def _create_attributed_response(
        self,
        request: dict[str, Any],
        *,
        purpose: str,
        item_count: int,
        client: OpenAI | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        """Create one model response and record only its billing metadata."""
        model = str(request.get("model") or "unknown")
        try:
            active_client = client or self.client
            active_request = dict(request)
            if timeout_seconds is not None:
                active_request["timeout"] = max(1, timeout_seconds)
            response = active_client.responses.create(**active_request)
        except Exception as exc:
            record_openai_usage(
                operation="responses.create",
                purpose=purpose,
                model=model,
                error=exc,
                item_count=item_count,
            )
            raise
        if response_has_usage(response):
            record_openai_usage(
                operation="responses.create",
                purpose=purpose,
                model=model,
                response=response,
                item_count=item_count,
            )
        return response

    def _append_sources_section(self, body: str, citations: list[Citation]) -> str:
        if not citations:
            return body.strip()

        joined_sources = "\n".join(
            [
                "Sources",
                *[
                    f"- [{citation.document_id}] {citation.title}"
                    + (f" ({citation.category})" if citation.category else "")
                    for citation in citations
                ],
            ]
        )
        normalized_body = body.strip()
        separator = "\n\n" if normalized_body else ""
        return f"{normalized_body}{separator}{joined_sources}".strip()

    def _parse_workbook_payload(self, raw_output: str) -> dict[str, Any]:
        normalized = str(raw_output or "").strip()
        if not normalized:
            raise RuntimeError("The model returned an empty workbook response.")

        if normalized.startswith("```"):
            normalized = re.sub(r"^```(?:json)?\s*|\s*```$", "", normalized, flags=re.IGNORECASE | re.DOTALL).strip()

        for candidate in (normalized, self._extract_json_candidate(normalized)):
            if not candidate:
                continue
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload

        raise RuntimeError("The model returned an invalid workbook structure.")

    def _extract_json_candidate(self, value: str) -> str | None:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end < start:
            return None
        return value[start:end + 1]

    def _normalize_workbook_payload(
        self,
        payload: dict[str, Any],
        *,
        fallback_title: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        workbook_title = str(payload.get("workbook_title") or fallback_title or DEFAULT_WORKBOOK_NAME).strip()
        raw_sheets = payload.get("sheets")
        if not isinstance(raw_sheets, list) or not raw_sheets:
            raise RuntimeError("The model did not return any spreadsheet sheets.")

        sheets: list[dict[str, Any]] = []
        for index, sheet in enumerate(raw_sheets[:5]):
            if not isinstance(sheet, dict):
                continue
            name = str(sheet.get("name") or f"Sheet {index + 1}").strip() or f"Sheet {index + 1}"
            rows = self._normalize_sheet_rows(sheet.get("rows"))
            if not rows:
                continue
            sheets.append(
                {
                    "name": name,
                    "rows": rows[:250],
                }
            )

        if not sheets:
            raise RuntimeError("The model returned spreadsheet sheets without usable rows.")
        return workbook_title, sheets

    def _normalize_sheet_rows(self, raw_rows: Any) -> list[list[str | int | float | bool]]:
        if not isinstance(raw_rows, list):
            return []

        meaningful_rows = [
            row
            for row in raw_rows
            if row not in (None, "", [], {})
        ]
        if not meaningful_rows:
            return []

        if all(isinstance(row, dict) for row in meaningful_rows):
            headers: list[str] = []
            seen_headers: set[str] = set()
            for row in meaningful_rows:
                for key in row.keys():
                    header = str(key).strip() or f"Column {len(headers) + 1}"
                    normalized_key = header.lower()
                    if normalized_key in seen_headers:
                        continue
                    seen_headers.add(normalized_key)
                    headers.append(header)
            if not headers:
                return []
            return [
                headers,
                *[
                    [self._normalize_cell_value(row.get(header, "")) for header in headers]
                    for row in meaningful_rows[:240]
                ],
            ]

        normalized_rows: list[list[str | int | float | bool]] = []
        for row in meaningful_rows[:240]:
            if isinstance(row, (list, tuple)):
                normalized_rows.append([self._normalize_cell_value(cell) for cell in row])
            else:
                normalized_rows.append([self._normalize_cell_value(row)])
        return normalized_rows

    def _normalize_cell_value(self, value: Any) -> str | int | float | bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=True)
        return str(value)

    def _build_sources_sheet(self, citations: list[Citation]) -> dict[str, Any]:
        rows: list[list[str]] = [["Document ID", "Title", "Category", "Excerpt"]]
        rows.extend(
            [
                [
                    citation.document_id,
                    citation.title,
                    citation.category or "",
                    citation.excerpt or "",
                ]
                for citation in citations
            ]
        )
        return {"name": SOURCE_SHEET_NAME, "rows": rows}

    def _build_docx_bytes(self, *, title: str, content: str) -> bytes:
        paragraphs = [title.strip(), "", *content.splitlines()]
        document_xml = self._build_docx_document_xml(paragraphs)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/word/document.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                    "</Types>"
                ),
            )
            archive.writestr(
                "_rels/.rels",
                (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                    'Target="word/document.xml"/>'
                    "</Relationships>"
                ),
            )
            archive.writestr("word/document.xml", document_xml)
        return buffer.getvalue()

    def _build_pdf_bytes(self, *, title: str, content: str) -> bytes:
        buffer = io.BytesIO()
        styles = getSampleStyleSheet()
        body_style = ParagraphStyle(
            "AskJennyBody",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=15,
            textColor=colors.HexColor("#263238"),
            spaceAfter=7,
        )
        title_style = ParagraphStyle(
            "AskJennyTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=27,
            alignment=TA_LEFT,
            textColor=colors.HexColor("#173F35"),
            spaceAfter=18,
        )
        heading_style = ParagraphStyle(
            "AskJennyHeading",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=18,
            textColor=colors.HexColor("#1E5A49"),
            spaceBefore=10,
            spaceAfter=7,
            keepWithNext=True,
        )
        subheading_style = ParagraphStyle(
            "AskJennySubheading",
            parent=styles["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=11.5,
            leading=15,
            textColor=colors.HexColor("#294C43"),
            spaceBefore=7,
            spaceAfter=5,
            keepWithNext=True,
        )
        source_style = ParagraphStyle(
            "AskJennySource",
            parent=body_style,
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#52635E"),
            leftIndent=8,
        )
        bullet_style = ParagraphStyle(
            "AskJennyBullet",
            parent=body_style,
            leftIndent=16,
            firstLineIndent=-10,
            spaceAfter=3,
        )
        document = SimpleDocTemplate(
            buffer,
            pagesize=LETTER,
            rightMargin=0.72 * inch,
            leftMargin=0.72 * inch,
            topMargin=0.78 * inch,
            bottomMargin=0.72 * inch,
            title=title,
            author="Ask Jenny",
            subject="Generated from the internal document library",
        )
        story: list[Any] = [
            Paragraph(self._pdf_inline_markup(title), title_style),
            Spacer(1, 2),
        ]
        lines = str(content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        index = 0
        in_sources = False
        while index < len(lines):
            raw_line = lines[index].strip()
            if not raw_line:
                story.append(Spacer(1, 5))
                index += 1
                continue
            if raw_line.lower() == "sources":
                if story:
                    story.append(PageBreak())
                story.append(Paragraph("Sources", heading_style))
                in_sources = True
                index += 1
                continue
            table_rows, consumed = self._parse_pdf_markdown_table(lines, index)
            if table_rows:
                story.append(self._build_pdf_table(table_rows, document.width, body_style))
                story.append(Spacer(1, 8))
                index += consumed
                continue
            bullet_match = re.match(r"^(?:[-*]|\u2022)\s+(.+)$", raw_line)
            if bullet_match:
                while index < len(lines):
                    match = re.match(r"^(?:[-*]|\u2022)\s+(.+)$", lines[index].strip())
                    if not match:
                        break
                    story.append(
                        Paragraph(
                            f"- {self._pdf_inline_markup(match.group(1))}",
                            source_style if in_sources else bullet_style,
                        )
                    )
                    index += 1
                story.append(Spacer(1, 3))
                continue
            heading_text, heading_level = self._classify_pdf_heading(raw_line)
            if heading_level:
                story.append(
                    Paragraph(
                        self._pdf_inline_markup(heading_text),
                        heading_style if heading_level == 1 else subheading_style,
                    )
                )
            else:
                paragraph_lines = [raw_line]
                index += 1
                while index < len(lines):
                    candidate = lines[index].strip()
                    if (
                        not candidate
                        or candidate.lower() == "sources"
                        or re.match(r"^(?:[-*]|\u2022)\s+", candidate)
                        or self._classify_pdf_heading(candidate)[1]
                        or self._parse_pdf_markdown_table(lines, index)[0]
                    ):
                        break
                    paragraph_lines.append(candidate)
                    index += 1
                story.append(
                    Paragraph(
                        self._pdf_inline_markup(" ".join(paragraph_lines)),
                        source_style if in_sources else body_style,
                    )
                )
                continue
            index += 1

        document.build(
            story,
            onFirstPage=lambda canvas, doc: self._draw_pdf_page_frame(canvas, doc, title),
            onLaterPages=lambda canvas, doc: self._draw_pdf_page_frame(canvas, doc, title),
        )
        return buffer.getvalue()

    def _draw_pdf_page_frame(self, canvas: Any, document: Any, title: str) -> None:
        canvas.saveState()
        page_width, page_height = LETTER
        header_text = title if len(title) <= 72 else f"{title[:69].rstrip()}..."
        canvas.setStrokeColor(colors.HexColor("#D6E2DE"))
        canvas.setLineWidth(0.5)
        canvas.line(document.leftMargin, page_height - 0.48 * inch, page_width - document.rightMargin, page_height - 0.48 * inch)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#657670"))
        canvas.drawString(document.leftMargin, page_height - 0.38 * inch, header_text)
        page_label = f"Page {document.page}"
        canvas.drawRightString(page_width - document.rightMargin, 0.38 * inch, page_label)
        canvas.setStrokeColor(colors.HexColor("#D6E2DE"))
        canvas.line(document.leftMargin, 0.52 * inch, page_width - document.rightMargin, 0.52 * inch)
        canvas.restoreState()

    def _classify_pdf_heading(self, line: str) -> tuple[str, int]:
        markdown_heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        if markdown_heading:
            return markdown_heading.group(2).strip(), 1 if len(markdown_heading.group(1)) <= 2 else 2
        if len(line) <= 80 and line.endswith(":"):
            return line[:-1].strip(), 2
        if (
            len(line) <= 72
            and not line.endswith((".", "?", "!", ";"))
            and len(line.split()) <= 9
        ):
            return line, 1
        return line, 0

    def _parse_pdf_markdown_table(
        self,
        lines: list[str],
        start_index: int,
    ) -> tuple[list[list[str]], int]:
        if start_index + 1 >= len(lines):
            return [], 0
        header = lines[start_index].strip()
        separator = lines[start_index + 1].strip()
        if "|" not in header or not re.match(r"^\|?\s*:?-{3,}", separator):
            return [], 0

        def split_row(value: str) -> list[str]:
            return [cell.strip() for cell in value.strip().strip("|").split("|")]

        rows = [split_row(header)]
        index = start_index + 2
        while index < len(lines) and "|" in lines[index] and lines[index].strip():
            row = split_row(lines[index])
            if len(row) == len(rows[0]):
                rows.append(row)
            index += 1
        return (rows, index - start_index) if len(rows) > 1 else ([], 0)

    def _build_pdf_table(
        self,
        rows: list[list[str]],
        available_width: float,
        body_style: ParagraphStyle,
    ) -> Table:
        column_count = max(1, len(rows[0]))
        widths = [1.0] * column_count
        for column_index in range(column_count):
            longest = max(
                (
                    stringWidth(
                        re.sub(r"[*_`]", "", row[column_index])[:80],
                        "Helvetica",
                        9,
                    )
                    for row in rows
                ),
                default=1.0,
            )
            widths[column_index] = max(0.75 * inch, min(longest + 18, 2.8 * inch))
        total_width = sum(widths)
        if total_width > available_width:
            scale = available_width / total_width
            widths = [width * scale for width in widths]
        cell_style = ParagraphStyle(
            "AskJennyTableCell",
            parent=body_style,
            fontSize=8.5,
            leading=11,
            spaceAfter=0,
        )
        header_cell_style = ParagraphStyle(
            "AskJennyTableHeaderCell",
            parent=cell_style,
            fontName="Helvetica-Bold",
            textColor=colors.white,
        )
        table_data = [
            [
                Paragraph(
                    self._pdf_inline_markup(cell),
                    header_cell_style if row_index == 0 else cell_style,
                )
                for cell in row
            ]
            for row_index, row in enumerate(rows)
        ]
        table = Table(table_data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E5A49")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD8D4")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F8F6")]),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        return table

    def _pdf_inline_markup(self, value: str) -> str:
        escaped = xml_escape(str(value or "").replace("\u2011", "-"))
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
        escaped = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", escaped)
        escaped = re.sub(r"`([^`]+?)`", r'<font name="Courier">\1</font>', escaped)
        return escaped

    def _build_docx_document_xml(self, paragraphs: list[str]) -> str:
        body = []
        for paragraph in paragraphs:
            normalized = str(paragraph or "").replace("\t", "    ")
            if normalized:
                runs = f'<w:r><w:t xml:space="preserve">{xml_escape(normalized)}</w:t></w:r>'
            else:
                runs = "<w:r/>"
            body.append(f"<w:p>{runs}</w:p>")
        body.append(
            "<w:sectPr>"
            '<w:pgSz w:w="12240" w:h="15840"/>'
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
            'w:header="720" w:footer="720" w:gutter="0"/>'
            "</w:sectPr>"
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            'xmlns:o="urn:schemas-microsoft-com:office:office" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
            'xmlns:v="urn:schemas-microsoft-com:vml" '
            'xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing" '
            'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
            'xmlns:w10="urn:schemas-microsoft-com:office:word" '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
            'xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup" '
            'xmlns:wpi="http://schemas.microsoft.com/office/word/2010/wordprocessingInk" '
            'xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml" '
            'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
            'mc:Ignorable="w14 wp14">'
            f"<w:body>{''.join(body)}</w:body>"
            "</w:document>"
        )

    def _build_xlsx_bytes(self, *, workbook_title: str, sheets: list[dict[str, Any]]) -> bytes:
        normalized_sheets = self._normalize_sheet_names(sheets)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", self._build_xlsx_content_types_xml(len(normalized_sheets)))
            archive.writestr("_rels/.rels", self._build_xlsx_root_relationships_xml())
            archive.writestr("xl/workbook.xml", self._build_xlsx_workbook_xml(workbook_title, normalized_sheets))
            archive.writestr("xl/_rels/workbook.xml.rels", self._build_xlsx_workbook_relationships_xml(len(normalized_sheets)))
            for index, sheet in enumerate(normalized_sheets, start=1):
                archive.writestr(
                    f"xl/worksheets/sheet{index}.xml",
                    self._build_xlsx_sheet_xml(sheet["rows"]),
                )
        return buffer.getvalue()

    def _normalize_sheet_names(self, sheets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        used_names: set[str] = set()
        for index, sheet in enumerate(sheets, start=1):
            raw_name = str(sheet.get("name") or f"Sheet {index}").strip() or f"Sheet {index}"
            safe_name = self._sanitize_sheet_name(raw_name, used_names)
            normalized.append({"name": safe_name, "rows": sheet["rows"]})
            used_names.add(safe_name.lower())
        return normalized

    def _sanitize_sheet_name(self, name: str, used_names: set[str]) -> str:
        sanitized = re.sub(r"[\[\]\:\*\?/\\]", "-", name).strip().strip("'")
        sanitized = sanitized[:31].strip() or "Sheet"
        candidate = sanitized
        suffix = 2
        while candidate.lower() in used_names:
            suffix_value = f" {suffix}"
            candidate = f"{sanitized[: max(1, 31 - len(suffix_value))]}{suffix_value}".strip()
            suffix += 1
        return candidate

    def _build_xlsx_content_types_xml(self, sheet_count: int) -> str:
        overrides = [
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
            *[
                f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for index in range(1, sheet_count + 1)
            ],
        ]
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f"{''.join(overrides)}"
            "</Types>"
        )

    def _build_xlsx_root_relationships_xml(self) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            "</Relationships>"
        )

    def _build_xlsx_workbook_xml(self, workbook_title: str, sheets: list[dict[str, Any]]) -> str:
        sheet_nodes = [
            f'<sheet name="{xml_escape(sheet["name"])}" sheetId="{index}" r:id="rId{index}"/>'
            for index, sheet in enumerate(sheets, start=1)
        ]
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<workbook xmlns="{SPREADSHEET_MAIN_NS}" xmlns:r="{OFFICE_DOCUMENT_REL_NS}">'
            f"<sheets>{''.join(sheet_nodes)}</sheets>"
            "</workbook>"
        )

    def _build_xlsx_workbook_relationships_xml(self, sheet_count: int) -> str:
        relationships = [
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, sheet_count + 1)
        ]
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{''.join(relationships)}"
            "</Relationships>"
        )

    def _build_xlsx_sheet_xml(self, rows: list[list[str | int | float | bool]]) -> str:
        row_nodes = []
        for row_index, row in enumerate(rows, start=1):
            cell_nodes = []
            for column_index, value in enumerate(row, start=1):
                cell_reference = f"{self._xlsx_column_name(column_index)}{row_index}"
                cell_nodes.append(self._build_xlsx_cell_xml(cell_reference, value))
            row_nodes.append(f'<row r="{row_index}">{"".join(cell_nodes)}</row>')
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<worksheet xmlns="{SPREADSHEET_MAIN_NS}">'
            f"<sheetData>{''.join(row_nodes)}</sheetData>"
            "</worksheet>"
        )

    def _build_xlsx_cell_xml(self, cell_reference: str, value: str | int | float | bool) -> str:
        if isinstance(value, bool):
            return f'<c r="{cell_reference}" t="b"><v>{"1" if value else "0"}</v></c>'
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f'<c r="{cell_reference}"><v>{value}</v></c>'
        normalized = str(value or "")
        return (
            f'<c r="{cell_reference}" t="inlineStr"><is><t xml:space="preserve">'
            f"{xml_escape(normalized)}</t></is></c>"
        )

    def _xlsx_column_name(self, column_index: int) -> str:
        result = []
        current = max(1, column_index)
        while current > 0:
            current, remainder = divmod(current - 1, 26)
            result.append(chr(65 + remainder))
        return "".join(reversed(result))

    def _derive_title_from_instructions(self, instructions: str) -> str:
        first_line = next((line.strip() for line in instructions.splitlines() if line.strip()), "")
        if not first_line:
            return DEFAULT_TEXT_DOCUMENT_NAME
        return first_line[:80]

    def _build_download_filename(self, title: str, output_format: GeneratedDocumentFormat) -> str:
        stem = self._slugify(title) or (DEFAULT_WORKBOOK_NAME if output_format == "xlsx" else DEFAULT_TEXT_DOCUMENT_NAME)
        return f"{stem}.{output_format}"

    def _slugify(self, value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "").strip().lower())
        return normalized.strip("-")[:80]

    def _build_success_message(self, filename: str, citations: list[Citation]) -> str:
        document_count = len(citations)
        noun = "document" if document_count == 1 else "documents"
        return f"Generated {filename} from {document_count} library {noun}."
