from __future__ import annotations

from typing import Protocol, TypedDict

from app.models import ReasoningMode


class ChatReasoningProfile(TypedDict):
    model: str
    effort: str


class ChatReasoningSettings(Protocol):
    openai_standard_model: str
    openai_maximum_model: str
    openai_standard_reasoning_effort: str
    openai_maximum_reasoning_effort: str


def get_chat_reasoning_profiles(
    settings: ChatReasoningSettings,
) -> dict[ReasoningMode, ChatReasoningProfile]:
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
    return get_chat_reasoning_profiles(settings)[reasoning_mode]
