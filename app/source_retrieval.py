"""Safely download public HTTPS PDF sources for chat retrieval.

Every redirect is validated and private, loopback, link-local, and otherwise
non-public IP addresses are rejected.  Those checks are the SSRF boundary that
prevents a user-supplied URL from reaching services on the local network.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from pathlib import PurePosixPath
import re
import socket
import time
from typing import Callable
from urllib.parse import unquote, urljoin, urlparse

import httpx


PDF_MIME_TYPE = "application/pdf"
_MAX_REDIRECTS = 5


@dataclass(frozen=True)
class RetrievedSourceDocument:
    """Validated PDF bytes plus the final URL and download metadata."""
    filename: str
    mime_type: str
    content_bytes: bytes
    source_url: str


class SourceDocumentRetriever:
    """Bounded HTTP retriever that permits only public HTTPS PDF responses."""
    def __init__(
        self,
        *,
        timeout_seconds: int,
        max_bytes: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout_seconds = max(1, timeout_seconds)
        self._max_bytes = max(1024, max_bytes)
        self._transport = transport

    def retrieve_pdf(
        self,
        url: str,
        filename: str | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> RetrievedSourceDocument:
        current_url = str(url or "").strip()
        deadline = time.monotonic() + self._timeout_seconds
        with httpx.Client(
            follow_redirects=False,
            headers={
                "Accept": "application/pdf",
                "User-Agent": "AskJenny/1.0 source-document-retriever",
            },
            transport=self._transport,
        ) as client:
            for redirect_count in range(_MAX_REDIRECTS + 1):
                if cancel_check is not None:
                    cancel_check()
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    raise ValueError("The source PDF download exceeded its total time limit.")
                self._validate_public_https_url(current_url)
                with client.stream(
                    "GET",
                    current_url,
                    timeout=httpx.Timeout(max(0.1, remaining_seconds)),
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect_count >= _MAX_REDIRECTS:
                            raise ValueError("The source document redirected too many times.")
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("The source document redirect had no destination.")
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            declared_length = int(content_length)
                        except ValueError as exc:
                            raise ValueError(
                                "The source PDF returned an invalid content length."
                            ) from exc
                        if declared_length > self._max_bytes:
                            raise ValueError(
                                f"The source PDF exceeds the {self._max_bytes // (1024 * 1024)} MB limit."
                            )
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        if cancel_check is not None:
                            cancel_check()
                        if time.monotonic() >= deadline:
                            raise ValueError(
                                "The source PDF download exceeded its total time limit."
                            )
                        content.extend(chunk)
                        if len(content) > self._max_bytes:
                            raise ValueError(
                                f"The source PDF exceeds the {self._max_bytes // (1024 * 1024)} MB limit."
                            )
                    content_bytes = bytes(content)
                    if not content_bytes.startswith(b"%PDF-"):
                        raise ValueError("The retrieved source is not an original PDF file.")
                    resolved_filename = self._resolve_filename(
                        filename,
                        current_url,
                        response.headers.get("content-disposition"),
                    )
                    return RetrievedSourceDocument(
                        filename=resolved_filename,
                        mime_type=PDF_MIME_TYPE,
                        content_bytes=content_bytes,
                        source_url=current_url,
                    )
        raise ValueError("The source PDF could not be retrieved.")

    @staticmethod
    def _validate_public_https_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https":
            raise ValueError("Source documents must use a public HTTPS URL.")
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("The source document URL is not valid.")
        if parsed.port not in {None, 443}:
            raise ValueError("Source document URLs must use the standard HTTPS port.")
        if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
            raise ValueError("Private or local source document URLs are not allowed.")
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    parsed.hostname,
                    443,
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror as exc:
            raise ValueError("The source document host could not be resolved.") from exc
        if not addresses:
            raise ValueError("The source document host could not be resolved.")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise ValueError("Private or local source document URLs are not allowed.")

    @staticmethod
    def _resolve_filename(
        requested_filename: str | None,
        url: str,
        content_disposition: str | None,
    ) -> str:
        candidates = [requested_filename]
        if content_disposition:
            encoded_match = re.search(
                r"filename\*=UTF-8''([^;]+)",
                content_disposition,
                flags=re.IGNORECASE,
            )
            simple_match = re.search(
                r'filename="?([^";]+)"?',
                content_disposition,
                flags=re.IGNORECASE,
            )
            candidates.append(
                unquote(encoded_match.group(1))
                if encoded_match
                else simple_match.group(1)
                if simple_match
                else None
            )
        candidates.append(PurePosixPath(urlparse(url).path).name)
        for candidate in candidates:
            normalized = re.sub(
                r"[^A-Za-z0-9._ -]+",
                "-",
                str(candidate or "").strip(),
            ).strip(" .-")
            if not normalized:
                continue
            if not normalized.lower().endswith(".pdf"):
                normalized = f"{normalized}.pdf"
            return normalized[:180]
        return "source-document.pdf"
