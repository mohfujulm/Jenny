"""Perform the lightweight network probe reported by the health endpoint.

The probe intentionally treats an HTTP authentication error as reachable: the
question here is whether the OpenAI host can be contacted, not whether the
application's API key is valid.
"""

from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


OPENAI_NETWORK_CHECK_URL = "https://api.openai.com/v1/models"


def check_openai_network_access(*, timeout_seconds: float = 5.0) -> dict[str, object]:
    """Return a serializable reachability result, latency, and diagnostic detail."""
    checked_at = datetime.now(timezone.utc).isoformat()
    started_at = perf_counter()
    request = Request(
        OPENAI_NETWORK_CHECK_URL,
        headers={"User-Agent": "team-knowledge-agent-network-check/1.0"},
        method="GET",
    )

    try:
        with urlopen(request, timeout=max(1.0, timeout_seconds)) as response:
            status_code = int(response.status)
        reachable = 200 <= status_code < 500
        detail = (
            "OpenAI API network access is available."
            if reachable
            else f"OpenAI API returned HTTP {status_code}."
        )
    except HTTPError as exc:
        status_code = int(exc.code)
        reachable = status_code == 401
        detail = (
            "OpenAI API network access is available."
            if reachable
            else f"OpenAI API returned HTTP {status_code}."
        )
    except (TimeoutError, URLError, OSError) as exc:
        status_code = None
        reachable = False
        reason = getattr(exc, "reason", None) or str(exc) or exc.__class__.__name__
        detail = f"Cannot reach the OpenAI API: {reason}"

    return {
        "reachable": reachable,
        "status": "ok" if reachable else "alert",
        "detail": detail,
        "status_code": status_code,
        "checked_at": checked_at,
        "latency_ms": round((perf_counter() - started_at) * 1000),
    }
