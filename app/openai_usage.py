"""Write privacy-safe attribution records for paid OpenAI operations.

The JSON Lines file is intentionally local application data.  Each row records
only billing metadata and coarse work counts; prompts, document identifiers,
document text, API keys, and response content are never accepted by this API.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
from threading import Lock
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OPENAI_USAGE_LOG_PATH = ROOT_DIR / "app" / "data" / "openai_usage.jsonl"

logger = logging.getLogger("app.openai_usage")
_write_lock = Lock()
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def get_openai_usage_log_path() -> Path:
    """Return the configured local JSONL path for OpenAI usage attribution."""
    configured = os.getenv("OPENAI_USAGE_LOG_PATH", "").strip()
    if not configured:
        return DEFAULT_OPENAI_USAGE_LOG_PATH

    path = Path(configured).expanduser()
    return path if path.is_absolute() else ROOT_DIR / path


def response_has_usage(response: object | None) -> bool:
    """Whether an SDK response carries the token-usage object we can attribute."""
    return _read_value(response, "usage") is not None


def record_openai_usage(
    *,
    operation: str,
    purpose: str,
    model: str,
    response: object | None = None,
    error: BaseException | None = None,
    item_count: int | None = None,
    page_count: int | None = None,
    chunk_count: int | None = None,
) -> bool:
    """Append one safe usage event without disrupting the operation being logged.

    ``response`` is inspected only for SDK billing metadata and a request ID.
    Error messages are deliberately excluded because an upstream service may
    echo request content into them.
    """
    event: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "operation": str(operation),
        "purpose": str(purpose),
        "model": str(model),
        "status": "error" if error is not None else "success",
    }

    usage = _read_value(response, "usage")
    input_tokens = _integer_value(usage, "input_tokens", "prompt_tokens")
    output_tokens = _integer_value(usage, "output_tokens", "completion_tokens")
    total_tokens = _integer_value(usage, "total_tokens")
    if input_tokens is not None:
        event["input_tokens"] = input_tokens
    if output_tokens is not None:
        event["output_tokens"] = output_tokens
    if total_tokens is not None:
        event["total_tokens"] = total_tokens

    request_id = _safe_code(
        _read_value(response, "_request_id", "request_id")
        or _read_value(error, "request_id", "_request_id")
    )
    if request_id:
        event["request_id"] = request_id

    for name, value in (
        ("item_count", item_count),
        ("page_count", page_count),
        ("chunk_count", chunk_count),
    ):
        normalized_count = _nonnegative_integer(value)
        if normalized_count is not None:
            event[name] = normalized_count

    if error is not None:
        event["error"] = _safe_error(error)

    path = get_openai_usage_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n"
        with _write_lock:
            with path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(serialized)
        return True
    except OSError:
        logger.exception("Could not append OpenAI usage metadata to %s.", path)
        return False


def _read_value(value: object | None, *names: str) -> Any:
    if value is None:
        return None
    for name in names:
        if isinstance(value, Mapping):
            candidate = value.get(name)
        else:
            candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return None


def _integer_value(value: object | None, *names: str) -> int | None:
    return _nonnegative_integer(_read_value(value, *names))


def _nonnegative_integer(value: object | None) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if normalized is None or normalized < 0:
        return None
    return normalized


def _safe_code(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized if _SAFE_CODE_RE.fullmatch(normalized) else None


def _safe_error(error: BaseException) -> dict[str, Any]:
    """Return diagnostic error metadata that cannot contain request content."""
    payload: dict[str, Any] = {"type": type(error).__name__}
    status_code = _nonnegative_integer(_read_value(error, "status_code"))
    error_code = _safe_code(_read_value(error, "code"))
    if status_code is not None:
        payload["status_code"] = status_code
    if error_code:
        payload["code"] = error_code
    return payload
