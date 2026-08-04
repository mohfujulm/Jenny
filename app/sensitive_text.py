"""Redact credential-like lines before text reaches model-facing workflows.

This is a deliberately conservative, line-oriented safety net.  It does not try
to recognize or retain the secret value; the entire suspicious line is replaced
so labels, usernames, and tokens cannot leak through partial matching.
"""

from __future__ import annotations

import re


SENSITIVE_LINE_RE = re.compile(
    r"\b("
    r"password|passwd|pwd|passcode|"
    r"api[\s_-]*key|secret|token|private[\s_-]*key|"
    r"recovery[\s_-]*code|session[\s_-]*cookie|"
    r"login[\s_-]*credentials|login[\s_-]*username|"
    r"user[\s_-]*account[\s_-]*login|username"
    r")\b",
    re.IGNORECASE,
)

REDACTION_NOTICE = "[REDACTED sensitive credential-like content]"


def redact_sensitive_text(value: str | None) -> str | None:
    """Replace lines containing credential vocabulary with a fixed notice."""
    if value is None:
        return None

    redacted_lines: list[str] = []
    last_was_redacted = False
    for line in value.splitlines() or [value]:
        if SENSITIVE_LINE_RE.search(line):
            if not last_was_redacted:
                redacted_lines.append(REDACTION_NOTICE)
            last_was_redacted = True
            continue
        redacted_lines.append(line)
        last_was_redacted = False

    return "\n".join(redacted_lines)
