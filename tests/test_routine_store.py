"""Verify atomic quota, ownership, scheduling, and failure controls."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.models import ContextFilter, RoutineDefinitionRequest
from app.routine_store import (
    RoutineConflictError,
    RoutineNotFoundError,
    RoutinePolicy,
    RoutineQuotaExceededError,
    RoutineStore,
)


def _policy(**overrides: int | bool) -> RoutinePolicy:
    values: dict[str, int | bool] = {
        "enabled": True,
        "max_per_user": 2,
        "max_concurrent_global": 2,
        "max_runs_per_user_daily": 2,
        "max_runs_global_daily": 4,
        "max_runs_per_user_monthly": 4,
        "max_runs_global_monthly": 8,
        "max_reserved_units_per_user_daily": 20_000,
        "max_reserved_units_global_daily": 50_000,
        "max_input_budget": 8_000,
        "chat_max_output_tokens": 1_000,
        "document_max_output_tokens": 2_000,
        "max_context_chars": 3_000,
        "max_documents": 2,
        "max_consecutive_failures": 2,
        "run_retention_days": 90,
    }
    values.update(overrides)
    return RoutinePolicy(**values)


def _definition(**overrides: object) -> RoutineDefinitionRequest:
    values: dict[str, object] = {
        "name": "Daily project note",
        "instructions": "Summarize project changes and unresolved decisions.",
        "output_format": "chat",
        "schedule_kind": "daily",
        "schedule_hour": 9,
        "schedule_minute": 0,
        "timezone": "UTC",
        "context_filter": ContextFilter(document_ids=["doc-1"]),
        "enabled": True,
    }
    values.update(overrides)
    return RoutineDefinitionRequest(**values)


class RoutineStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owner = TemporaryDirectory(prefix="routine-store-")
        self.store = RoutineStore(
            Path(self.owner.name) / "routines.sqlite",
            _policy(),
        )

    def tearDown(self) -> None:
        self.owner.cleanup()

    def test_routines_and_documents_are_owner_scoped(self) -> None:
        routine = self.store.create_routine("user-a", _definition())
        with self.assertRaises(RoutineNotFoundError):
            self.store.get_routine("user-b", routine.routine_id)

        claim = self.store.claim_run(
            owner_user_id="user-a",
            routine_id=routine.routine_id,
            trigger="manual",
            reserved_units=9_000,
        )
        self.store.complete_success(
            claim.run_id,
            response_text="done",
            citations=[],
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            filename="result.pdf",
            mime_type="application/pdf",
            document_bytes=b"pdf",
        )
        with self.assertRaises(RoutineNotFoundError):
            self.store.get_run_document("user-b", claim.run_id)
        self.assertEqual(
            self.store.get_run_document("user-a", claim.run_id)[2],
            b"pdf",
        )

    def test_definition_cap_is_enforced_transactionally(self) -> None:
        self.store.create_routine("user-a", _definition(name="One"))
        self.store.create_routine("user-a", _definition(name="Two"))
        with self.assertRaises(RoutineQuotaExceededError):
            self.store.create_routine("user-a", _definition(name="Three"))

    def test_definition_rejects_unbounded_or_blank_untrusted_fields(self) -> None:
        with self.assertRaises(ValueError):
            _definition(name="   ")
        with self.assertRaises(ValueError):
            _definition(context_filter=ContextFilter(document_ids=["x" * 513]))

    def test_overlapping_runs_are_rejected(self) -> None:
        one = self.store.create_routine("user-a", _definition(name="One"))
        two = self.store.create_routine("user-a", _definition(name="Two"))
        self.store.claim_run(
            owner_user_id="user-a",
            routine_id=one.routine_id,
            trigger="manual",
            reserved_units=9_000,
        )
        with self.assertRaises(RoutineConflictError):
            self.store.claim_run(
                owner_user_id="user-a",
                routine_id=two.routine_id,
                trigger="manual",
                reserved_units=9_000,
            )

    def test_failed_runs_consume_daily_count_and_reserved_budget(self) -> None:
        routine = self.store.create_routine("user-a", _definition())
        first = self.store.claim_run(
            owner_user_id="user-a",
            routine_id=routine.routine_id,
            trigger="manual",
            reserved_units=9_000,
        )
        self.store.complete_failure(first.run_id, "deliberate_failure")
        second = self.store.claim_run(
            owner_user_id="user-a",
            routine_id=routine.routine_id,
            trigger="manual",
            reserved_units=9_000,
        )
        self.store.complete_failure(second.run_id, "deliberate_failure")
        with self.assertRaises(RoutineQuotaExceededError):
            self.store.claim_run(
                owner_user_id="user-a",
                routine_id=routine.routine_id,
                trigger="manual",
                reserved_units=1,
            )

    def test_global_limits_bound_spend_across_multiple_accounts(self) -> None:
        store = RoutineStore(
            Path(self.owner.name) / "global-limits.sqlite",
            _policy(max_runs_global_daily=1),
        )
        first_routine = store.create_routine("user-a", _definition(name="First"))
        second_routine = store.create_routine("user-b", _definition(name="Second"))
        first = store.claim_run(
            owner_user_id="user-a",
            routine_id=first_routine.routine_id,
            trigger="manual",
            reserved_units=9_000,
        )
        store.complete_failure(first.run_id, "failed_but_charged")
        with self.assertRaises(RoutineQuotaExceededError):
            store.claim_run(
                owner_user_id="user-b",
                routine_id=second_routine.routine_id,
                trigger="manual",
                reserved_units=9_000,
            )

    def test_deleting_routine_scrubs_results_without_erasing_quota_ledger(self) -> None:
        store = RoutineStore(
            Path(self.owner.name) / "delete-ledger.sqlite",
            _policy(max_runs_per_user_daily=1),
        )
        routine = store.create_routine("user-a", _definition(name="Delete me"))
        claim = store.claim_run(
            owner_user_id="user-a",
            routine_id=routine.routine_id,
            trigger="manual",
            reserved_units=9_000,
        )
        store.complete_success(
            claim.run_id,
            response_text="private result",
            citations=[],
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            filename="private.pdf",
            mime_type="application/pdf",
            document_bytes=b"private",
        )
        store.delete_routine("user-a", routine.routine_id)

        self.assertEqual(store.list_routines("user-a"), [])
        self.assertEqual(store.list_runs("user-a"), [])
        with self.assertRaises(RoutineNotFoundError):
            store.get_run_document("user-a", claim.run_id)

        replacement = store.create_routine("user-a", _definition(name="Replacement"))
        with self.assertRaises(RoutineQuotaExceededError):
            store.claim_run(
                owner_user_id="user-a",
                routine_id=replacement.routine_id,
                trigger="manual",
                reserved_units=9_000,
            )

    def test_deleting_run_output_scrubs_content_but_preserves_quota_ledger(self) -> None:
        store = RoutineStore(
            Path(self.owner.name) / "delete-output-ledger.sqlite",
            _policy(max_runs_per_user_daily=1),
        )
        routine = store.create_routine("user-a", _definition())
        claim = store.claim_run(
            owner_user_id="user-a",
            routine_id=routine.routine_id,
            trigger="manual",
            reserved_units=9_000,
        )
        store.complete_success(
            claim.run_id,
            response_text="private result",
            citations=[],
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            filename="private.pdf",
            mime_type="application/pdf",
            document_bytes=b"private",
        )

        store.delete_run_output("user-a", claim.run_id)

        self.assertEqual(store.list_runs("user-a"), [])
        with self.assertRaises(RoutineNotFoundError):
            store.get_run_document("user-a", claim.run_id)
        with self.assertRaises(RoutineQuotaExceededError):
            store.claim_run(
                owner_user_id="user-a",
                routine_id=routine.routine_id,
                trigger="manual",
                reserved_units=9_000,
            )

    def test_active_run_cannot_be_deleted_to_orphan_paid_work(self) -> None:
        routine = self.store.create_routine("user-a", _definition())
        self.store.claim_run(
            owner_user_id="user-a",
            routine_id=routine.routine_id,
            trigger="manual",
            reserved_units=9_000,
        )
        with self.assertRaises(RoutineConflictError):
            self.store.delete_routine("user-a", routine.routine_id)

    def test_repeated_failures_auto_pause_routine(self) -> None:
        routine = self.store.create_routine("user-a", _definition())
        for index in range(2):
            claim = self.store.claim_run(
                owner_user_id="user-a",
                routine_id=routine.routine_id,
                trigger="manual",
                reserved_units=9_000,
            )
            auto_paused = self.store.complete_failure(claim.run_id, "failure")
            self.assertEqual(auto_paused, index == 1)
        self.assertFalse(self.store.get_routine("user-a", routine.routine_id).enabled)

    def test_admin_pause_blocks_new_claims(self) -> None:
        routine = self.store.create_routine("user-a", _definition())
        self.store.set_system_paused(True)
        with self.assertRaises(RoutineConflictError):
            self.store.claim_run(
                owner_user_id="user-a",
                routine_id=routine.routine_id,
                trigger="manual",
                reserved_units=9_000,
            )

    def test_weekly_schedule_is_calculated_in_requested_timezone(self) -> None:
        definition = _definition(
            schedule_kind="weekly",
            schedule_weekday=0,
            schedule_hour=9,
        )
        next_run = RoutineStore.calculate_next_run(
            definition,
            datetime(2026, 8, 4, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(next_run, "2026-08-10T09:00:00.000Z")

        eastern = _definition(
            schedule_kind="daily",
            schedule_hour=9,
            timezone="America/New_York",
        )
        eastern_next = RoutineStore.calculate_next_run(
            eastern,
            datetime(2026, 8, 4, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(eastern_next, "2026-08-04T13:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
