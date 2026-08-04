"""Map user-facing reasoning choices to concrete OpenAI model settings.

The UI exposes stable labels (``standard`` and ``maximum``); this module keeps
provider model names and effort levels in configuration rather than scattering
them through request handling code.
"""

from __future__ import annotations

from typing import Protocol, TypedDict

from app.models import ReasoningMode


class ChatReasoningProfile(TypedDict):
    """Concrete model and reasoning effort used for one chat request."""
    model: str
    effort: str


class ChatReasoningSettings(Protocol):
    """Minimal settings interface needed to construct chat profiles."""
    openai_standard_model: str
    openai_maximum_model: str
    openai_standard_reasoning_effort: str
    openai_maximum_reasoning_effort: str


def get_chat_reasoning_profiles(
    settings: ChatReasoningSettings,
) -> dict[ReasoningMode, ChatReasoningProfile]:
    """Return all selectable profiles keyed by the UI's stable mode names."""
    return {
        "standard": {
            "model": settings.openai_standard_model,
            "effort": settings.openai_standard_reasoning_effort,
        },
        "maximum": {
            "model": settings.openai_maximum_model,
            "effort": settings.openai_maximum_reasoning_effort,
        },
    }


def get_chat_reasoning_profile(
    settings: ChatReasoningSettings,
    reasoning_mode: ReasoningMode,
) -> ChatReasoningProfile:
    """Resolve one mode, falling back to ``standard`` for unknown input."""
    return get_chat_reasoning_profiles(settings)[reasoning_mode]
