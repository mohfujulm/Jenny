"""Deterministic input budgeting for stateless Responses API requests.

The provider does not expose a preflight tokenizer for every multimodal model.
This module therefore uses UTF-8 bytes as a conservative upper bound for text
tokens and assigns an explicit charge to each bounded-detail image.  The result
is stable across tokenizer/model upgrades and, unlike a characters-per-token
heuristic, cannot underestimate ordinary text because a token represents at
least one encoded byte.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


_IMAGE_URL_PREFIX = "data:image/"
_FIXED_REQUEST_OVERHEAD_UNITS = 1_024


class RequestInputBudgetExceeded(ValueError):
    """Raised before an API call when required current-turn state is too large."""

    def __init__(self, estimated_units: int, maximum_units: int) -> None:
        super().__init__(
            "The current request exceeds the configured model-input budget "
            f"({estimated_units:,} > {maximum_units:,})."
        )
        self.estimated_units = estimated_units
        self.maximum_units = maximum_units


@dataclass(frozen=True)
class BoundedRequestInput:
    """The history/memory selected for one provider request."""

    history: list[Any]
    conversation_memory: str
    estimated_units: int
    dropped_history_items: int = 0
    memory_was_dropped: bool = False


class ResponsesRequestBudget:
    """Fit optional context while treating current-turn state as indivisible."""

    def __init__(
        self,
        maximum_units: int,
        *,
        image_units: int = 4_096,
    ) -> None:
        self.maximum_units = max(8_000, int(maximum_units))
        self.image_units = max(512, int(image_units))

    def fit(
        self,
        *,
        instructions: str,
        tools: list[dict[str, Any]],
        history: list[Any],
        conversation_memory: str,
        protected_history_start: int,
    ) -> BoundedRequestInput:
        """Drop optional context until the complete request fits the envelope.

        ``protected_history_start`` marks the current user input and everything
        produced after it. Those items are never silently truncated because
        doing so can break function-call IDs or encrypted reasoning continuity.
        """
        selected_history = list(history)
        protected_start = min(
            max(0, int(protected_history_start)),
            len(selected_history),
        )
        selected_memory = str(conversation_memory or "")
        dropped_items = 0

        estimated = self.estimate(
            instructions=instructions,
            tools=tools,
            history=selected_history,
            conversation_memory=selected_memory,
        )
        while estimated > self.maximum_units and protected_start > 0:
            drop_count = self._oldest_complete_turn_size(
                selected_history,
                protected_start,
            )
            if drop_count <= 0:
                break
            del selected_history[:drop_count]
            protected_start -= drop_count
            dropped_items += drop_count
            estimated = self.estimate(
                instructions=instructions,
                tools=tools,
                history=selected_history,
                conversation_memory=selected_memory,
            )

        memory_was_dropped = False
        if estimated > self.maximum_units and selected_memory:
            selected_memory = ""
            memory_was_dropped = True
            estimated = self.estimate(
                instructions=instructions,
                tools=tools,
                history=selected_history,
                conversation_memory=selected_memory,
            )

        if estimated > self.maximum_units:
            raise RequestInputBudgetExceeded(estimated, self.maximum_units)

        return BoundedRequestInput(
            history=selected_history,
            conversation_memory=selected_memory,
            estimated_units=estimated,
            dropped_history_items=dropped_items,
            memory_was_dropped=memory_was_dropped,
        )

    def estimate(
        self,
        *,
        instructions: str,
        tools: list[dict[str, Any]],
        history: list[Any],
        conversation_memory: str,
    ) -> int:
        image_count = 0

        def normalize(value: Any) -> Any:
            nonlocal image_count
            if isinstance(value, str):
                if value.startswith(_IMAGE_URL_PREFIX) and ";base64," in value[:96]:
                    image_count += 1
                    return "[bounded image input]"
                return value
            if value is None or isinstance(value, (bool, int, float)):
                return value
            if isinstance(value, dict):
                return {str(key): normalize(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [normalize(item) for item in value]
            model_dump = getattr(value, "model_dump", None)
            if callable(model_dump):
                return normalize(model_dump(mode="json"))
            if hasattr(value, "__dict__"):
                return normalize(
                    {
                        key: item
                        for key, item in vars(value).items()
                        if not key.startswith("_")
                    }
                )
            return str(value)

        payload = normalize(
            {
                "instructions": instructions,
                "tools": tools,
                "input": history,
                "conversation_memory": conversation_memory,
            }
        )
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        # Reserve covers the model/reasoning/text/store/include fields plus the
        # fixed conversation-memory guidance wrapped around selected excerpts.
        return (
            len(encoded)
            + (image_count * self.image_units)
            + _FIXED_REQUEST_OVERHEAD_UNITS
        )

    @staticmethod
    def _oldest_complete_turn_size(history: list[Any], protected_start: int) -> int:
        """Return an old user-led turn size without crossing protected state."""
        if protected_start <= 0:
            return 0
        drop_count = 1
        while drop_count < protected_start:
            candidate = history[drop_count]
            role = candidate.get("role") if isinstance(candidate, dict) else None
            if role == "user":
                break
            drop_count += 1
        return drop_count
