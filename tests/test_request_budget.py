"""Verify deterministic whole-request input budgeting."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from app.request_budget import RequestInputBudgetExceeded, ResponsesRequestBudget


class ResponsesRequestBudgetTests(unittest.TestCase):
    def test_drops_old_complete_turns_before_relevant_memory(self) -> None:
        budget = ResponsesRequestBudget(8_000)
        result = budget.fit(
            instructions="system",
            tools=[],
            history=[
                {"role": "user", "content": "old question " + ("x" * 5_000)},
                {"role": "assistant", "content": "old answer " + ("y" * 3_000)},
                {"role": "user", "content": "current question"},
            ],
            conversation_memory="relevant decision",
            protected_history_start=2,
        )

        self.assertEqual(result.dropped_history_items, 2)
        self.assertEqual(result.history[0]["content"], "current question")
        self.assertEqual(result.conversation_memory, "relevant decision")
        self.assertLessEqual(result.estimated_units, budget.maximum_units)

    def test_drops_optional_memory_before_failing_current_turn(self) -> None:
        budget = ResponsesRequestBudget(8_000)
        result = budget.fit(
            instructions="system",
            tools=[],
            history=[{"role": "user", "content": "current question"}],
            conversation_memory="m" * 8_000,
            protected_history_start=0,
        )

        self.assertTrue(result.memory_was_dropped)
        self.assertEqual(result.conversation_memory, "")

    def test_never_silently_truncates_required_current_turn_state(self) -> None:
        budget = ResponsesRequestBudget(8_000)
        with self.assertRaises(RequestInputBudgetExceeded):
            budget.fit(
                instructions="system",
                tools=[],
                history=[
                    {"role": "user", "content": "current"},
                    SimpleNamespace(
                        type="reasoning",
                        encrypted_content="r" * 10_000,
                    ),
                ],
                conversation_memory="",
                protected_history_start=0,
            )

    def test_image_bytes_are_not_counted_as_text_but_each_image_is_charged(self) -> None:
        budget = ResponsesRequestBudget(20_000, image_units=4_096)
        without_image = budget.estimate(
            instructions="system",
            tools=[],
            history=[{"role": "user", "content": "review"}],
            conversation_memory="",
        )
        with_image = budget.estimate(
            instructions="system",
            tools=[],
            history=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "review"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64," + ("A" * 100_000),
                            "detail": "high",
                        },
                    ],
                }
            ],
            conversation_memory="",
        )

        self.assertGreaterEqual(with_image - without_image, 4_096)
        self.assertLess(with_image, 10_000)


if __name__ == "__main__":
    unittest.main()
