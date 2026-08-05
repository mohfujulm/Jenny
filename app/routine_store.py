"""Crash-safe routine definitions, execution claims, and quota accounting.

The store is deliberately independent of the scheduler and OpenAI client.  A
single ``BEGIN IMMEDIATE`` transaction decides whether a run may start and
records its worst-case token reservation before any paid work is attempted.
That makes concurrent workers, restarts, and deliberately failing requests
unable to bypass the configured limits.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models import Citation, ContextFilter, RoutineDefinitionRequest


class RoutineError(ValueError):
    """Base class for safe, user-facing routine errors."""


class RoutineNotFoundError(RoutineError):
    pass


class RoutineConflictError(RoutineError):
    pass


class RoutineQuotaExceededError(RoutineError):
    pass


@dataclass(frozen=True)
class RoutinePolicy:
    enabled: bool
    max_per_user: int
    max_concurrent_global: int
    max_runs_per_user_daily: int
    max_runs_global_daily: int
    max_runs_per_user_monthly: int
    max_runs_global_monthly: int
    max_reserved_units_per_user_daily: int
    max_reserved_units_global_daily: int
    max_input_budget: int
    chat_max_output_tokens: int
    document_max_output_tokens: int
    max_context_chars: int
    max_documents: int
    max_consecutive_failures: int
    run_retention_days: int

    @classmethod
    def from_settings(cls, settings: object) -> "RoutinePolicy":
        positive = lambda name, default: max(1, int(getattr(settings, name, default)))
        return cls(
            enabled=bool(getattr(settings, "routines_enabled", True)),
            max_per_user=positive("routines_max_per_user", 10),
            max_concurrent_global=positive("routines_max_concurrent_global", 2),
            max_runs_per_user_daily=positive("routines_max_runs_per_user_daily", 10),
            max_runs_global_daily=positive("routines_max_runs_global_daily", 50),
            max_runs_per_user_monthly=positive("routines_max_runs_per_user_monthly", 100),
            max_runs_global_monthly=positive("routines_max_runs_global_monthly", 1_000),
            max_reserved_units_per_user_daily=positive(
                "routines_max_reserved_units_per_user_daily", 250_000
            ),
            max_reserved_units_global_daily=positive(
                "routines_max_reserved_units_global_daily", 2_000_000
            ),
            max_input_budget=positive("routines_max_input_budget", 24_000),
            chat_max_output_tokens=positive("routines_chat_max_output_tokens", 1_200),
            document_max_output_tokens=positive(
                "routines_document_max_output_tokens", 3_000
            ),
            max_context_chars=positive("routines_max_context_chars", 6_000),
            max_documents=positive("routines_max_documents", 3),
            max_consecutive_failures=positive(
                "routines_max_consecutive_failures", 3
            ),
            run_retention_days=positive("routines_run_retention_days", 90),
        )

    def public_summary(self) -> dict[str, int | bool]:
        return {
            "enabled": self.enabled,
            "max_routines_per_user": self.max_per_user,
            "max_runs_per_user_daily": self.max_runs_per_user_daily,
            "max_runs_per_user_monthly": self.max_runs_per_user_monthly,
            "max_input_budget": self.max_input_budget,
            "chat_max_output_tokens": self.chat_max_output_tokens,
            "document_max_output_tokens": self.document_max_output_tokens,
            "max_documents": self.max_documents,
            "run_retention_days": self.run_retention_days,
        }


@dataclass(frozen=True)
class RoutineRecord:
    routine_id: str
    owner_user_id: str
    name: str
    instructions: str
    output_format: str
    source_mode: str
    schedule_kind: str
    schedule_hour: int
    schedule_minute: int
    schedule_weekday: int | None
    timezone: str
    context_filter: ContextFilter
    enabled: bool
    consecutive_failures: int
    last_run_at: str | None
    next_run_at: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "routine_id": self.routine_id,
            "owner_user_id": self.owner_user_id,
            "name": self.name,
            "instructions": self.instructions,
            "output_format": self.output_format,
            "source_mode": self.source_mode,
            "schedule_kind": self.schedule_kind,
            "schedule_hour": self.schedule_hour,
            "schedule_minute": self.schedule_minute,
            "schedule_weekday": self.schedule_weekday,
            "timezone": self.timezone,
            "context_filter": self.context_filter,
            "enabled": self.enabled,
            "consecutive_failures": self.consecutive_failures,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class RoutineRunClaim:
    run_id: str
    routine: RoutineRecord
    trigger: str
    reserved_units: int
    started_at: str


class RoutineStore:
    """SQLite-backed routines with atomic admission control."""

    def __init__(self, database_path: Path, policy: RoutinePolicy) -> None:
        self._database_path = Path(database_path)
        self.policy = policy
        self._initialize_lock = threading.Lock()
        self._initialize()

    def create_routine(
        self,
        owner_user_id: str,
        definition: RoutineDefinitionRequest,
    ) -> RoutineRecord:
        now = self._now()
        next_run = self.calculate_next_run(definition, datetime.now(timezone.utc))
        routine_id = str(uuid.uuid4())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            retention_cutoff = self._iso(
                datetime.now(timezone.utc)
                - timedelta(days=self.policy.run_retention_days)
            )
            connection.execute(
                """
                DELETE FROM routines
                WHERE deleted_at IS NOT NULL AND deleted_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM routine_runs
                      WHERE routine_runs.routine_id = routines.routine_id
                  )
                """,
                (retention_cutoff,),
            )
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM routines WHERE owner_user_id = ? AND deleted_at IS NULL",
                    (owner_user_id,),
                ).fetchone()[0]
            )
            if count >= self.policy.max_per_user:
                connection.rollback()
                raise RoutineQuotaExceededError(
                    f"Each account may have at most {self.policy.max_per_user} routines."
                )
            day_start = self._iso(
                datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
            )
            user_creations = self._scalar(
                connection,
                "SELECT COUNT(*) FROM routines WHERE owner_user_id = ? AND created_at >= ?",
                (owner_user_id, day_start),
            )
            global_creations = self._scalar(
                connection,
                "SELECT COUNT(*) FROM routines WHERE created_at >= ?",
                (day_start,),
            )
            if user_creations >= self.policy.max_per_user * 2:
                connection.rollback()
                raise RoutineQuotaExceededError("Your daily routine-creation limit has been reached.")
            if global_creations >= self.policy.max_runs_global_daily * 2:
                connection.rollback()
                raise RoutineQuotaExceededError("The shared daily routine-creation limit has been reached.")
            connection.execute(
                """
                INSERT INTO routines (
                    routine_id, owner_user_id, name, instructions, output_format,
                    source_mode,
                    schedule_kind, schedule_hour, schedule_minute,
                    schedule_weekday, timezone, context_filter_json, enabled,
                    consecutive_failures, next_run_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    routine_id,
                    owner_user_id,
                    definition.name.strip(),
                    definition.instructions.strip(),
                    definition.output_format,
                    definition.source_mode,
                    definition.schedule_kind,
                    definition.schedule_hour,
                    definition.schedule_minute,
                    definition.schedule_weekday,
                    definition.timezone,
                    definition.context_filter.model_dump_json(),
                    int(definition.enabled),
                    next_run,
                    now,
                    now,
                ),
            )
            connection.commit()
        return self.get_routine(owner_user_id, routine_id)

    def update_routine(
        self,
        owner_user_id: str,
        routine_id: str,
        definition: RoutineDefinitionRequest,
    ) -> RoutineRecord:
        now = self._now()
        next_run = self.calculate_next_run(definition, datetime.now(timezone.utc))
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE routines
                SET name = ?, instructions = ?, output_format = ?, source_mode = ?,
                    schedule_kind = ?, schedule_hour = ?, schedule_minute = ?,
                    schedule_weekday = ?, timezone = ?, context_filter_json = ?,
                    enabled = ?, next_run_at = ?, updated_at = ?
                WHERE routine_id = ? AND owner_user_id = ?
                  AND deleted_at IS NULL
                """,
                (
                    definition.name.strip(),
                    definition.instructions.strip(),
                    definition.output_format,
                    definition.source_mode,
                    definition.schedule_kind,
                    definition.schedule_hour,
                    definition.schedule_minute,
                    definition.schedule_weekday,
                    definition.timezone,
                    definition.context_filter.model_dump_json(),
                    int(definition.enabled),
                    next_run,
                    now,
                    routine_id,
                    owner_user_id,
                ),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise RoutineNotFoundError("Routine not found.")
        return self.get_routine(owner_user_id, routine_id)

    def set_enabled(
        self, owner_user_id: str, routine_id: str, enabled: bool
    ) -> RoutineRecord:
        routine = self.get_routine(owner_user_id, routine_id)
        definition = RoutineDefinitionRequest(
            name=routine.name,
            instructions=routine.instructions,
            output_format=routine.output_format,
            source_mode=routine.source_mode,
            schedule_kind=routine.schedule_kind,
            schedule_hour=routine.schedule_hour,
            schedule_minute=routine.schedule_minute,
            schedule_weekday=routine.schedule_weekday,
            timezone=routine.timezone,
            context_filter=routine.context_filter,
            enabled=enabled,
        )
        updated = self.update_routine(owner_user_id, routine_id, definition)
        if enabled and updated.consecutive_failures:
            with closing(self._connect()) as connection:
                connection.execute(
                    """
                    UPDATE routines SET consecutive_failures = 0, updated_at = ?
                    WHERE routine_id = ? AND owner_user_id = ?
                    """,
                    (self._now(), routine_id, owner_user_id),
                )
                connection.commit()
            updated = self.get_routine(owner_user_id, routine_id)
        return updated

    def delete_routine(self, owner_user_id: str, routine_id: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT routine_id FROM routines
                WHERE routine_id = ? AND owner_user_id = ? AND deleted_at IS NULL
                """,
                (routine_id, owner_user_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RoutineNotFoundError("Routine not found.")
            active = self._scalar(
                connection,
                "SELECT COUNT(*) FROM routine_runs WHERE routine_id = ? AND status = 'running'",
                (routine_id,),
            )
            if active:
                connection.rollback()
                raise RoutineConflictError("Wait for the active routine run to finish before deleting it.")
            now = self._now()
            # Soft-delete the definition so its minimal run ledger can continue
            # enforcing daily/monthly quotas. User content and generated files
            # are scrubbed immediately and no longer appear in owner queries.
            connection.execute(
                """
                UPDATE routines
                SET name = '[deleted routine]', instructions = '[deleted]',
                    context_filter_json = '{"folder_ids":[],"document_ids":[]}',
                    enabled = 0, deleted_at = ?, updated_at = ?
                WHERE routine_id = ?
                """,
                (now, now, routine_id),
            )
            connection.execute(
                """
                UPDATE routine_runs
                SET routine_name = '[deleted routine]', response_text = NULL,
                    filename = NULL, mime_type = NULL, document_blob = NULL,
                    citations_json = '[]'
                WHERE routine_id = ?
                """,
                (routine_id,),
            )
            connection.commit()

    def get_routine(self, owner_user_id: str, routine_id: str) -> RoutineRecord:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM routines
                WHERE routine_id = ? AND owner_user_id = ? AND deleted_at IS NULL
                """,
                (routine_id, owner_user_id),
            ).fetchone()
        if row is None:
            raise RoutineNotFoundError("Routine not found.")
        return self._row_to_routine(row)

    def list_routines(self, owner_user_id: str) -> list[RoutineRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM routines
                WHERE owner_user_id = ? AND deleted_at IS NULL
                ORDER BY name COLLATE NOCASE, created_at
                """,
                (owner_user_id,),
            ).fetchall()
        return [self._row_to_routine(row) for row in rows]

    def list_due_routines(self, limit: int = 20) -> list[RoutineRecord]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM routines
                WHERE enabled = 1 AND deleted_at IS NULL AND next_run_at <= ?
                ORDER BY next_run_at
                LIMIT ?
                """,
                (self._now(), max(1, min(int(limit), 100))),
            ).fetchall()
        return [self._row_to_routine(row) for row in rows]

    def defer_scheduled_routine(self, routine: RoutineRecord) -> None:
        """Advance a quota-blocked due item instead of retrying every poll."""
        definition = self._definition_from_record(routine)
        next_run = self.calculate_next_run(definition, datetime.now(timezone.utc))
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE routines SET next_run_at = ?, updated_at = ?
                WHERE routine_id = ? AND enabled = 1 AND deleted_at IS NULL
                """,
                (next_run, self._now(), routine.routine_id),
            )
            connection.commit()

    def claim_run(
        self,
        *,
        owner_user_id: str,
        routine_id: str,
        trigger: str,
        reserved_units: int,
    ) -> RoutineRunClaim:
        if trigger not in {"manual", "scheduled"}:
            raise ValueError("Unsupported routine trigger.")
        reserved = max(1, int(reserved_units))
        now_dt = datetime.now(timezone.utc)
        now = self._iso(now_dt)
        day_start = self._iso(now_dt.replace(hour=0, minute=0, second=0, microsecond=0))
        month_start = self._iso(now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
        run_id = str(uuid.uuid4())

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM routine_runs
                WHERE status != 'running' AND started_at < ?
                """,
                (
                    self._iso(
                        now_dt - timedelta(days=self.policy.run_retention_days)
                    ),
                ),
            )
            connection.execute(
                """
                DELETE FROM routines
                WHERE deleted_at IS NOT NULL AND deleted_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM routine_runs
                      WHERE routine_runs.routine_id = routines.routine_id
                  )
                """,
                (
                    self._iso(
                        now_dt - timedelta(days=self.policy.run_retention_days)
                    ),
                ),
            )
            if self._system_paused(connection):
                connection.rollback()
                raise RoutineConflictError("Routine execution is paused by an administrator.")
            row = connection.execute(
                """
                SELECT * FROM routines
                WHERE routine_id = ? AND owner_user_id = ? AND deleted_at IS NULL
                """,
                (routine_id, owner_user_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RoutineNotFoundError("Routine not found.")
            routine = self._row_to_routine(row)
            if trigger == "scheduled" and not routine.enabled:
                connection.rollback()
                raise RoutineConflictError("This routine is paused.")

            active_user = self._scalar(
                connection,
                "SELECT COUNT(*) FROM routine_runs WHERE owner_user_id = ? AND status = 'running'",
                (owner_user_id,),
            )
            active_routine = self._scalar(
                connection,
                "SELECT COUNT(*) FROM routine_runs WHERE routine_id = ? AND status = 'running'",
                (routine_id,),
            )
            active_global = self._scalar(
                connection,
                "SELECT COUNT(*) FROM routine_runs WHERE status = 'running'",
                (),
            )
            if active_user or active_routine:
                connection.rollback()
                raise RoutineConflictError("A routine is already running for this account.")
            if active_global >= self.policy.max_concurrent_global:
                connection.rollback()
                raise RoutineConflictError("The routine worker is currently at capacity.")

            self._enforce_count_quota(
                connection, owner_user_id, day_start, self.policy.max_runs_per_user_daily,
                self.policy.max_runs_global_daily, "daily"
            )
            self._enforce_count_quota(
                connection, owner_user_id, month_start,
                self.policy.max_runs_per_user_monthly,
                self.policy.max_runs_global_monthly, "monthly"
            )
            self._enforce_unit_quota(connection, owner_user_id, day_start, reserved)

            connection.execute(
                """
                INSERT INTO routine_runs (
                    run_id, routine_id, owner_user_id, routine_name, trigger,
                    status, reserved_units, started_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    run_id,
                    routine_id,
                    owner_user_id,
                    routine.name,
                    trigger,
                    reserved,
                    now,
                ),
            )
            if trigger == "scheduled":
                definition = self._definition_from_record(routine)
                next_run = self.calculate_next_run(
                    definition, now_dt + timedelta(seconds=1)
                )
                connection.execute(
                    "UPDATE routines SET next_run_at = ?, updated_at = ? WHERE routine_id = ?",
                    (next_run, now, routine_id),
                )
            connection.commit()
        return RoutineRunClaim(run_id, routine, trigger, reserved, now)

    def complete_success(
        self,
        run_id: str,
        *,
        response_text: str,
        citations: list[Citation],
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
        filename: str | None = None,
        mime_type: str | None = None,
        document_bytes: bytes | None = None,
    ) -> None:
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT routine_id FROM routine_runs WHERE run_id = ? AND status = 'running'",
                (run_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RoutineConflictError("Routine run is no longer active.")
            connection.execute(
                """
                UPDATE routine_runs
                SET status = 'succeeded', response_text = ?, citations_json = ?,
                    input_tokens = ?, output_tokens = ?, total_tokens = ?,
                    filename = ?, mime_type = ?, document_blob = ?, completed_at = ?
                WHERE run_id = ?
                """,
                (
                    response_text,
                    json.dumps([item.model_dump(mode="json") for item in citations]),
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    filename,
                    mime_type,
                    document_bytes,
                    now,
                    run_id,
                ),
            )
            connection.execute(
                """
                UPDATE routines
                SET consecutive_failures = 0, last_run_at = ?, updated_at = ?
                WHERE routine_id = ?
                """,
                (now, now, str(row["routine_id"])),
            )
            connection.commit()

    def complete_failure(self, run_id: str, error_code: str) -> bool:
        """Mark a failed run and return whether the routine was auto-paused."""
        safe_code = self._safe_error_code(error_code)
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT routine_id FROM routine_runs WHERE run_id = ? AND status = 'running'",
                (run_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            routine_id = str(row["routine_id"])
            connection.execute(
                """
                UPDATE routine_runs SET status = 'failed', error_code = ?, completed_at = ?
                WHERE run_id = ?
                """,
                (safe_code, now, run_id),
            )
            connection.execute(
                """
                UPDATE routines
                SET consecutive_failures = consecutive_failures + 1,
                    last_run_at = ?, updated_at = ?
                WHERE routine_id = ?
                """,
                (now, now, routine_id),
            )
            failures = self._scalar(
                connection,
                "SELECT consecutive_failures FROM routines WHERE routine_id = ?",
                (routine_id,),
            )
            paused = failures >= self.policy.max_consecutive_failures
            if paused:
                connection.execute(
                    "UPDATE routines SET enabled = 0, updated_at = ? WHERE routine_id = ?",
                    (now, routine_id),
                )
            connection.commit()
        return paused

    def recover_stale_runs(self, older_than_seconds: int) -> int:
        cutoff = self._iso(
            datetime.now(timezone.utc) - timedelta(seconds=max(60, older_than_seconds))
        )
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT run_id, routine_id FROM routine_runs
                WHERE status = 'running' AND started_at < ?
                """,
                (cutoff,),
            ).fetchall()
            connection.execute(
                """
                UPDATE routine_runs
                SET status = 'failed', error_code = 'worker_interrupted', completed_at = ?
                WHERE status = 'running' AND started_at < ?
                """,
                (now, cutoff),
            )
            for row in rows:
                connection.execute(
                    """
                    UPDATE routines
                    SET consecutive_failures = consecutive_failures + 1,
                        last_run_at = ?, updated_at = ?
                    WHERE routine_id = ?
                    """,
                    (now, now, str(row["routine_id"])),
                )
            connection.execute(
                """
                UPDATE routines SET enabled = 0, updated_at = ?
                WHERE consecutive_failures >= ?
                """,
                (now, self.policy.max_consecutive_failures),
            )
            connection.commit()
            return len(rows)

    def list_runs(self, owner_user_id: str, limit: int = 50) -> list[dict[str, object]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT run_id, routine_id, routine_name, trigger, status,
                       response_text, filename, mime_type,
                       document_blob IS NOT NULL AS has_document, citations_json,
                       input_tokens, output_tokens, total_tokens, reserved_units,
                       error_code, started_at, completed_at
                FROM routine_runs
                WHERE owner_user_id = ?
                  AND deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM routines
                      WHERE routines.routine_id = routine_runs.routine_id
                        AND routines.deleted_at IS NULL
                  )
                ORDER BY started_at DESC LIMIT ?
                """,
                (owner_user_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def get_run(self, owner_user_id: str, run_id: str) -> dict[str, object]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT run_id, routine_id, routine_name, trigger, status,
                       response_text, filename, mime_type,
                       document_blob IS NOT NULL AS has_document, citations_json,
                       input_tokens, output_tokens, total_tokens, reserved_units,
                       error_code, started_at, completed_at
                FROM routine_runs
                WHERE run_id = ? AND owner_user_id = ? AND deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM routines
                      WHERE routines.routine_id = routine_runs.routine_id
                        AND routines.deleted_at IS NULL
                  )
                """,
                (run_id, owner_user_id),
            ).fetchone()
        if row is None:
            raise RoutineNotFoundError("Routine result not found.")
        return self._row_to_run(row)

    def get_run_document(
        self, owner_user_id: str, run_id: str
    ) -> tuple[str, str, bytes]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT filename, mime_type, document_blob FROM routine_runs
                WHERE run_id = ? AND owner_user_id = ? AND status = 'succeeded'
                  AND deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM routines
                      WHERE routines.routine_id = routine_runs.routine_id
                        AND routines.deleted_at IS NULL
                  )
                """,
                (run_id, owner_user_id),
            ).fetchone()
        if row is None or row["document_blob"] is None:
            raise RoutineNotFoundError("Routine document not found.")
        return str(row["filename"]), str(row["mime_type"]), bytes(row["document_blob"])

    def delete_run_output(self, owner_user_id: str, run_id: str) -> None:
        """Permanently scrub an output while retaining its spent-budget ledger."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT status FROM routine_runs
                WHERE run_id = ? AND owner_user_id = ? AND deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM routines
                      WHERE routines.routine_id = routine_runs.routine_id
                        AND routines.deleted_at IS NULL
                  )
                """,
                (run_id, owner_user_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RoutineNotFoundError("Routine result not found.")
            if str(row["status"]) == "running":
                connection.rollback()
                raise RoutineConflictError("A running routine result cannot be deleted.")
            connection.execute(
                """
                UPDATE routine_runs
                SET response_text = NULL, filename = NULL, mime_type = NULL,
                    document_blob = NULL, citations_json = '[]', deleted_at = ?
                WHERE run_id = ?
                """,
                (self._now(), run_id),
            )
            connection.commit()

    def is_system_paused(self) -> bool:
        with closing(self._connect()) as connection:
            return self._system_paused(connection)

    def set_system_paused(self, paused: bool) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO routine_system_state (state_key, state_value, updated_at)
                VALUES ('paused', ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    state_value = excluded.state_value,
                    updated_at = excluded.updated_at
                """,
                ("1" if paused else "0", self._now()),
            )
            connection.commit()

    @staticmethod
    def calculate_next_run(
        definition: RoutineDefinitionRequest,
        after_utc: datetime,
    ) -> str:
        if after_utc.tzinfo is None:
            after_utc = after_utc.replace(tzinfo=timezone.utc)
        try:
            tz = timezone.utc if definition.timezone == "UTC" else ZoneInfo(definition.timezone)
        except ZoneInfoNotFoundError as exc:
            raise RoutineError("Unknown time zone.") from exc
        local_after = after_utc.astimezone(tz)
        candidate = local_after.replace(
            hour=definition.schedule_hour,
            minute=definition.schedule_minute,
            second=0,
            microsecond=0,
        )
        if definition.schedule_kind == "weekly":
            weekday = int(definition.schedule_weekday or 0)
            candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
        if candidate <= local_after:
            candidate += timedelta(days=7 if definition.schedule_kind == "weekly" else 1)
        return RoutineStore._iso(candidate.astimezone(timezone.utc))

    def _enforce_count_quota(
        self,
        connection: sqlite3.Connection,
        owner_user_id: str,
        since: str,
        user_maximum: int,
        global_maximum: int,
        label: str,
    ) -> None:
        user_count = self._scalar(
            connection,
            "SELECT COUNT(*) FROM routine_runs WHERE owner_user_id = ? AND started_at >= ?",
            (owner_user_id, since),
        )
        global_count = self._scalar(
            connection,
            "SELECT COUNT(*) FROM routine_runs WHERE started_at >= ?",
            (since,),
        )
        if user_count >= user_maximum:
            connection.rollback()
            raise RoutineQuotaExceededError(f"Your {label} routine-run limit has been reached.")
        if global_count >= global_maximum:
            connection.rollback()
            raise RoutineQuotaExceededError(f"The shared {label} routine-run limit has been reached.")

    def _enforce_unit_quota(
        self,
        connection: sqlite3.Connection,
        owner_user_id: str,
        since: str,
        requested: int,
    ) -> None:
        user_units = self._scalar(
            connection,
            "SELECT COALESCE(SUM(reserved_units), 0) FROM routine_runs WHERE owner_user_id = ? AND started_at >= ?",
            (owner_user_id, since),
        )
        global_units = self._scalar(
            connection,
            "SELECT COALESCE(SUM(reserved_units), 0) FROM routine_runs WHERE started_at >= ?",
            (since,),
        )
        if user_units + requested > self.policy.max_reserved_units_per_user_daily:
            connection.rollback()
            raise RoutineQuotaExceededError("Your daily routine token budget has been reached.")
        if global_units + requested > self.policy.max_reserved_units_global_daily:
            connection.rollback()
            raise RoutineQuotaExceededError("The shared daily routine token budget has been reached.")

    def _initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._initialize_lock, closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS routines (
                    routine_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    output_format TEXT NOT NULL CHECK(output_format IN ('chat','pdf','docx')),
                    source_mode TEXT NOT NULL DEFAULT 'internal' CHECK(source_mode IN ('internal','broader')),
                    schedule_kind TEXT NOT NULL CHECK(schedule_kind IN ('daily','weekly')),
                    schedule_hour INTEGER NOT NULL,
                    schedule_minute INTEGER NOT NULL,
                    schedule_weekday INTEGER,
                    timezone TEXT NOT NULL,
                    context_filter_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_run_at TEXT,
                    next_run_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_routines_owner ON routines(owner_user_id);
                CREATE INDEX IF NOT EXISTS idx_routines_due ON routines(enabled, next_run_at);
                CREATE INDEX IF NOT EXISTS idx_routines_deleted ON routines(deleted_at);

                CREATE TABLE IF NOT EXISTS routine_runs (
                    run_id TEXT PRIMARY KEY,
                    routine_id TEXT NOT NULL REFERENCES routines(routine_id) ON DELETE CASCADE,
                    owner_user_id TEXT NOT NULL,
                    routine_name TEXT NOT NULL,
                    trigger TEXT NOT NULL CHECK(trigger IN ('manual','scheduled')),
                    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
                    response_text TEXT,
                    filename TEXT,
                    mime_type TEXT,
                    document_blob BLOB,
                    citations_json TEXT NOT NULL DEFAULT '[]',
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    reserved_units INTEGER NOT NULL,
                    error_code TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    deleted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_owner_started ON routine_runs(owner_user_id, started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_runs_status ON routine_runs(status);
                CREATE INDEX IF NOT EXISTS idx_runs_started ON routine_runs(started_at);

                CREATE TABLE IF NOT EXISTS routine_system_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(routines)").fetchall()
            }
            if "deleted_at" not in columns:
                connection.execute("ALTER TABLE routines ADD COLUMN deleted_at TEXT")
            if "source_mode" not in columns:
                connection.execute(
                    "ALTER TABLE routines ADD COLUMN source_mode TEXT NOT NULL DEFAULT 'internal'"
                )
            run_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(routine_runs)").fetchall()
            }
            if "deleted_at" not in run_columns:
                connection.execute("ALTER TABLE routine_runs ADD COLUMN deleted_at TEXT")
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _row_to_routine(row: sqlite3.Row) -> RoutineRecord:
        return RoutineRecord(
            routine_id=str(row["routine_id"]),
            owner_user_id=str(row["owner_user_id"]),
            name=str(row["name"]),
            instructions=str(row["instructions"]),
            output_format=str(row["output_format"]),
            source_mode=str(row["source_mode"]),
            schedule_kind=str(row["schedule_kind"]),
            schedule_hour=int(row["schedule_hour"]),
            schedule_minute=int(row["schedule_minute"]),
            schedule_weekday=(None if row["schedule_weekday"] is None else int(row["schedule_weekday"])),
            timezone=str(row["timezone"]),
            context_filter=ContextFilter.model_validate_json(str(row["context_filter_json"])),
            enabled=bool(row["enabled"]),
            consecutive_failures=int(row["consecutive_failures"]),
            last_run_at=(None if row["last_run_at"] is None else str(row["last_run_at"])),
            next_run_at=str(row["next_run_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> dict[str, object]:
        try:
            citations = [Citation.model_validate(item) for item in json.loads(str(row["citations_json"] or "[]"))]
        except (ValueError, TypeError, json.JSONDecodeError):
            citations = []
        return {
            "run_id": str(row["run_id"]),
            "routine_id": str(row["routine_id"]),
            "routine_name": str(row["routine_name"]),
            "trigger": str(row["trigger"]),
            "status": str(row["status"]),
            "response_text": row["response_text"],
            "filename": row["filename"],
            "mime_type": row["mime_type"],
            "has_document": bool(row["has_document"]),
            "citations": citations,
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "total_tokens": row["total_tokens"],
            "reserved_units": int(row["reserved_units"]),
            "error_code": row["error_code"],
            "started_at": str(row["started_at"]),
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _definition_from_record(record: RoutineRecord) -> RoutineDefinitionRequest:
        return RoutineDefinitionRequest(
            name=record.name,
            instructions=record.instructions,
            output_format=record.output_format,
            source_mode=record.source_mode,
            schedule_kind=record.schedule_kind,
            schedule_hour=record.schedule_hour,
            schedule_minute=record.schedule_minute,
            schedule_weekday=record.schedule_weekday,
            timezone=record.timezone,
            context_filter=record.context_filter,
            enabled=record.enabled,
        )

    @staticmethod
    def _scalar(connection: sqlite3.Connection, query: str, parameters: tuple[object, ...]) -> int:
        return int(connection.execute(query, parameters).fetchone()[0])

    @staticmethod
    def _system_paused(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT state_value FROM routine_system_state WHERE state_key = 'paused'"
        ).fetchone()
        return bool(row is not None and str(row[0]) == "1")

    @staticmethod
    def _safe_error_code(value: str) -> str:
        normalized = "".join(
            character for character in str(value or "routine_failed")[:64]
            if character.isalnum() or character in "_.-"
        )
        return normalized or "routine_failed"

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _now() -> str:
        return RoutineStore._iso(datetime.now(timezone.utc))
