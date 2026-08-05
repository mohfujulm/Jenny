"""Cover account validation, legacy migration, password hashing, and sessions."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from app.user_store import (
    DuplicateUsernameError,
    InvalidCredentialsError,
    UserStore,
)


class UserStoreTests(unittest.TestCase):
    def _store(self, directory: str) -> UserStore:
        return UserStore(Path(directory) / "application.sqlite")

    def test_signup_account_authenticates_and_creates_session(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            created = store.create_user(
                username="  Person.Name ",
                display_name="  Person   Name  ",
                password="PortablePass1",
            )

            authenticated = store.authenticate(
                "person.name",
                "PortablePass1",
            )
            token = store.create_session(authenticated.user_id, 24)

            self.assertEqual(created.role, "member")
            self.assertEqual(created.display_name, "Person Name")
            self.assertEqual(store.get_user_for_session(token), authenticated)

    def test_rejects_wrong_password(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            store.create_user(
                username="person",
                display_name="Person",
                password="PortablePass1",
            )

            with self.assertRaises(InvalidCredentialsError):
                store.authenticate("person", "WrongPassword1")

    def test_rejects_duplicate_username_case_insensitively(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            store.create_user(
                username="person",
                display_name="First Person",
                password="PortablePass1",
            )

            with self.assertRaisesRegex(DuplicateUsernameError, "already exists"):
                store.create_user(
                    username="PERSON",
                    display_name="Second Person",
                    password="PortablePass1",
                )

    def test_default_administrator_is_idempotent_and_requires_password_change(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            first = store.ensure_default_admin(
                username="admin",
                display_name="Administrator",
                password="Administrator!1",
            )
            second = store.ensure_default_admin(
                username="admin",
                display_name="Administrator",
                password="Administrator!1",
            )

            self.assertEqual(first.user_id, second.user_id)
            self.assertEqual(first.role, "admin")
            self.assertTrue(first.must_change_password)
            self.assertEqual(
                store.authenticate(
                    "admin",
                    "Administrator!1",
                ).user_id,
                first.user_id,
            )

    def test_default_administrator_bootstrap_preserves_existing_account_state(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "application.sqlite"
            store = UserStore(database_path)
            original = store.ensure_default_admin(
                username="admin",
                display_name="Administrator",
                password="Administrator!1",
            )
            with closing(sqlite3.connect(database_path)) as connection:
                original_password_hash = connection.execute(
                    "SELECT password_hash FROM users WHERE user_id = ?",
                    (original.user_id,),
                ).fetchone()[0]
                connection.execute(
                    """
                    UPDATE users
                    SET display_name = 'Intentionally Demoted',
                        role = 'member', is_active = 0,
                        must_change_password = 0,
                        updated_at = '2026-08-01T00:00:00+00:00'
                    WHERE user_id = ?
                    """,
                    (original.user_id,),
                )
                connection.commit()

            preserved = store.ensure_default_admin(
                username="admin",
                display_name="Configured Administrator",
                password="DifferentPassword2",
            )

            self.assertEqual(preserved.display_name, "Intentionally Demoted")
            self.assertEqual(preserved.role, "member")
            self.assertFalse(preserved.is_active)
            self.assertFalse(preserved.must_change_password)
            self.assertEqual(preserved.updated_at, "2026-08-01T00:00:00+00:00")
            with closing(sqlite3.connect(database_path)) as connection:
                preserved_password_hash = connection.execute(
                    "SELECT password_hash FROM users WHERE user_id = ?",
                    (original.user_id,),
                ).fetchone()[0]
            self.assertEqual(preserved_password_hash, original_password_hash)

    def test_password_change_clears_first_login_requirement(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            admin = store.ensure_default_admin(
                username="admin",
                display_name="Administrator",
                password="Administrator!1",
            )

            updated = store.change_password(
                user_id=admin.user_id,
                current_password="Administrator!1",
                new_password="ChangedAdmin2",
            )

            self.assertFalse(updated.must_change_password)
            self.assertEqual(
                store.authenticate(
                    "admin",
                    "ChangedAdmin2",
                ).user_id,
                admin.user_id,
            )

    def test_migrates_existing_email_account_to_username_without_email_column(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "application.sqlite"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE users (
                        user_id TEXT PRIMARY KEY,
                        email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                        display_name TEXT NOT NULL,
                        role TEXT NOT NULL,
                        is_active INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.commit()
                connection.execute(
                    """
                    INSERT INTO users (
                        user_id, email, display_name, role, is_active,
                        created_at, updated_at
                    )
                    VALUES ('legacy-admin', 'admin@askjenny.local', 'Administrator',
                            'admin', 1, '2026-01-01', '2026-01-01')
                    """
                )
                connection.commit()

            store = UserStore(database_path)
            migrated = store.get_user("legacy-admin")
            with closing(sqlite3.connect(database_path)) as connection:
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(users)").fetchall()
                }

            self.assertEqual(migrated.username, "admin")
            self.assertIn("username", columns)
            self.assertNotIn("email", columns)

    def test_rejects_weak_password(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            with self.assertRaisesRegex(ValueError, "at least 10"):
                store.create_user(
                    username="person",
                    display_name="Person",
                    password="Short1A",
                )


if __name__ == "__main__":
    unittest.main()
