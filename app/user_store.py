"""SQLite-backed local accounts, password verification, and login sessions.

Passwords use salted PBKDF2-HMAC and constant-time comparison.  Raw browser
session tokens are returned once; only SHA-256 hashes are stored, limiting the
value of a copied database.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import uuid

from app.models import UserRole, UserSummary


_USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")
_PASSWORD_ITERATIONS = 600_000


class DuplicateUsernameError(ValueError):
    """Raised when normalized account names would collide."""
    pass


class InvalidCredentialsError(ValueError):
    """Raised without revealing whether the username or password was wrong."""
    pass


class UserStore:
    """Manage users and expiring authentication sessions in one SQLite file."""
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._lock = threading.Lock()
        self._initialize()

    def get_user(self, user_id: str) -> UserSummary | None:
        normalized_id = str(user_id or "").strip()
        if not normalized_id:
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT user_id, username, display_name, role, is_active,
                       must_change_password, created_at, updated_at
                FROM users
                WHERE user_id = ?
                """,
                (normalized_id,),
            ).fetchone()
        return None if row is None else self._row_to_user(row)

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        role: UserRole = "member",
        must_change_password: bool = False,
    ) -> UserSummary:
        normalized_username = self._normalize_username(username)
        normalized_name = self._normalize_display_name(display_name)
        normalized_password = self._validate_password(password)
        if role not in {"admin", "library_manager", "member"}:
            raise ValueError("Role must be admin, library manager, or member.")
        salt, password_hash = self._hash_password(normalized_password)
        user_id = str(uuid.uuid4())
        timestamp = self._now()

        with self._lock, closing(self._connect()) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO users (
                        user_id, username, display_name, role, is_active,
                        password_salt, password_hash, password_iterations,
                        must_change_password, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        normalized_username,
                        normalized_name,
                        role,
                        salt,
                        password_hash,
                        _PASSWORD_ITERATIONS,
                        int(must_change_password),
                        timestamp,
                        timestamp,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise DuplicateUsernameError(
                    f"An account with username `{normalized_username}` already exists."
                ) from exc

        created_user = self.get_user(user_id)
        if created_user is None:
            raise RuntimeError("The account was created but could not be loaded.")
        return created_user

    def ensure_default_admin(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
    ) -> UserSummary:
        normalized_username = self._normalize_username(username)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT user_id, password_hash FROM users WHERE username = ?",
                (normalized_username,),
            ).fetchone()
            if row is not None and row["password_hash"]:
                user_id = str(row["user_id"])
                connection.execute(
                    """
                    UPDATE users
                    SET display_name = ?, role = 'admin', is_active = 1, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        self._normalize_display_name(display_name),
                        self._now(),
                        user_id,
                    ),
                )
                connection.commit()
            elif row is not None:
                salt, password_hash = self._hash_password(self._validate_password(password))
                timestamp = self._now()
                connection.execute(
                    """
                    UPDATE users
                    SET display_name = ?, role = 'admin', is_active = 1,
                        password_salt = ?, password_hash = ?,
                        password_iterations = ?, must_change_password = 1,
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        self._normalize_display_name(display_name),
                        salt,
                        password_hash,
                        _PASSWORD_ITERATIONS,
                        timestamp,
                        str(row["user_id"]),
                    ),
                )
                connection.commit()
                user_id = str(row["user_id"])
            else:
                user_id = ""
        if not user_id:
            return self.create_user(
                username=normalized_username,
                display_name=display_name,
                password=password,
                role="admin",
                must_change_password=True,
            )
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("The default Administrator account could not be loaded.")
        return user

    def authenticate(self, username: str, password: str) -> UserSummary:
        normalized_username = self._normalize_username(username)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT user_id, username, display_name, role, is_active,
                       password_salt, password_hash, password_iterations,
                       must_change_password, created_at, updated_at
                FROM users
                WHERE username = ?
                """,
                (normalized_username,),
            ).fetchone()
        if row is None or not row["is_active"] or not row["password_hash"]:
            raise InvalidCredentialsError("Email or password is incorrect.")
        _, candidate_hash = self._hash_password(
            str(password),
            salt=str(row["password_salt"]),
            iterations=int(row["password_iterations"]),
        )
        if not hmac.compare_digest(candidate_hash, str(row["password_hash"])):
            raise InvalidCredentialsError("Email or password is incorrect.")
        return self._row_to_user(row)

    def create_session(self, user_id: str, ttl_hours: int) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = self._session_token_hash(token)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=max(1, ttl_hours))
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions (
                    session_token_hash, user_id, created_at, expires_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (token_hash, user_id, now.isoformat(), expires_at.isoformat()),
            )
            connection.commit()
        return token

    def get_user_for_session(self, token: str | None) -> UserSummary | None:
        if not token:
            return None
        token_hash = self._session_token_hash(token)
        now = self._now()
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT users.user_id, users.username, users.display_name, users.role,
                       users.is_active, users.must_change_password,
                       users.created_at, users.updated_at
                FROM auth_sessions
                JOIN users ON users.user_id = auth_sessions.user_id
                WHERE auth_sessions.session_token_hash = ?
                  AND auth_sessions.expires_at > ?
                  AND users.is_active = 1
                """,
                (token_hash, now),
            ).fetchone()
            connection.execute(
                "DELETE FROM auth_sessions WHERE expires_at <= ?",
                (now,),
            )
            connection.commit()
        return None if row is None else self._row_to_user(row)

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE session_token_hash = ?",
                (self._session_token_hash(token),),
            )
            connection.commit()

    def change_password(
        self,
        *,
        user_id: str,
        current_password: str,
        new_password: str,
    ) -> UserSummary:
        user = self.get_user(user_id)
        if user is None:
            raise InvalidCredentialsError("Account not found.")
        self.authenticate(user.username, current_password)
        normalized_password = self._validate_password(new_password)
        salt, password_hash = self._hash_password(normalized_password)
        timestamp = self._now()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE users
                SET password_salt = ?, password_hash = ?, password_iterations = ?,
                    must_change_password = 0, updated_at = ?
                WHERE user_id = ?
                """,
                (salt, password_hash, _PASSWORD_ITERATIONS, timestamp, user_id),
            )
            connection.commit()
        updated = self.get_user(user_id)
        if updated is None:
            raise RuntimeError("The updated account could not be loaded.")
        return updated

    def _initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as connection:
            users_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'"
            ).fetchone()
            if users_exists is None:
                self._create_users_table(connection)
            else:
                existing_columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(users)").fetchall()
                }
                if "username" not in existing_columns:
                    self._migrate_email_accounts(connection)

            existing_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            additions = {
                "password_salt": "TEXT",
                "password_hash": "TEXT",
                "password_iterations": "INTEGER",
                "must_change_password": "INTEGER NOT NULL DEFAULT 0",
            }
            for column_name, column_type in additions.items():
                if column_name not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE users ADD COLUMN {column_name} {column_type}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    session_token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS auth_sessions_user_id ON auth_sessions(user_id)"
            )
            connection.commit()

    @staticmethod
    def _create_users_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'library_manager', 'member')),
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                password_salt TEXT,
                password_hash TEXT,
                password_iterations INTEGER,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    def _migrate_email_accounts(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = OFF")
        legacy_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        legacy_users = connection.execute("SELECT * FROM users").fetchall()
        sessions_exist = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'auth_sessions'"
        ).fetchone()
        legacy_sessions = (
            connection.execute("SELECT * FROM auth_sessions").fetchall()
            if sessions_exist is not None
            else []
        )
        if sessions_exist is not None:
            connection.execute("DROP TABLE auth_sessions")
        connection.execute("ALTER TABLE users RENAME TO users_email_legacy")
        self._create_users_table(connection)

        assigned_usernames: set[str] = set()
        for row in legacy_users:
            username = self._username_from_legacy_email(
                str(row["email"]),
                assigned_usernames,
            )
            assigned_usernames.add(username)
            connection.execute(
                """
                INSERT INTO users (
                    user_id, username, display_name, role, is_active,
                    password_salt, password_hash, password_iterations,
                    must_change_password, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row["user_id"]),
                    username,
                    str(row["display_name"]),
                    str(row["role"]),
                    int(row["is_active"]),
                    row["password_salt"] if "password_salt" in legacy_columns else None,
                    row["password_hash"] if "password_hash" in legacy_columns else None,
                    row["password_iterations"] if "password_iterations" in legacy_columns else None,
                    int(row["must_change_password"])
                    if "must_change_password" in legacy_columns
                    else 0,
                    str(row["created_at"]),
                    str(row["updated_at"]),
                ),
            )
        connection.execute("DROP TABLE users_email_legacy")
        self._create_auth_sessions_table(connection)
        valid_user_ids = {str(row["user_id"]) for row in legacy_users}
        for session in legacy_sessions:
            if str(session["user_id"]) not in valid_user_ids:
                continue
            connection.execute(
                """
                INSERT INTO auth_sessions (
                    session_token_hash, user_id, created_at, expires_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(session["session_token_hash"]),
                    str(session["user_id"]),
                    str(session["created_at"]),
                    str(session["expires_at"]),
                ),
            )
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _create_auth_sessions_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_sessions (
                session_token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """
        )

    @staticmethod
    def _username_from_legacy_email(
        email: str,
        assigned_usernames: set[str],
    ) -> str:
        base = email.strip().lower().split("@", 1)[0]
        base = re.sub(r"[^a-z0-9._-]+", "-", base).strip("._-")
        if len(base) < 3:
            base = f"user-{base}".strip("-")
        base = base[:40]
        candidate = base
        suffix = 2
        while candidate in assigned_usernames:
            suffix_text = f"-{suffix}"
            candidate = f"{base[:40 - len(suffix_text)]}{suffix_text}"
            suffix += 1
        return candidate

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _normalize_username(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError(
                "Username must be 3–40 characters and use only letters, numbers, periods, underscores, or hyphens."
            )
        return normalized

    @staticmethod
    def _normalize_display_name(value: str) -> str:
        normalized = " ".join(str(value or "").split())
        if not normalized:
            raise ValueError("Display name is required.")
        if len(normalized) > 120:
            raise ValueError("Display name must be 120 characters or fewer.")
        return normalized

    @staticmethod
    def _validate_password(value: str) -> str:
        password = str(value or "")
        if len(password) < 10:
            raise ValueError("Password must be at least 10 characters.")
        if not re.search(r"[a-z]", password):
            raise ValueError("Password must include a lowercase letter.")
        if not re.search(r"[A-Z]", password):
            raise ValueError("Password must include an uppercase letter.")
        if not re.search(r"\d", password):
            raise ValueError("Password must include a number.")
        return password

    @staticmethod
    def _hash_password(
        password: str,
        *,
        salt: str | None = None,
        iterations: int = _PASSWORD_ITERATIONS,
    ) -> tuple[str, str]:
        encoded_salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(encoded_salt),
            iterations,
        ).hex()
        return encoded_salt, digest

    @staticmethod
    def _session_token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> UserSummary:
        return UserSummary(
            user_id=str(row["user_id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            role=str(row["role"]),
            is_active=bool(row["is_active"]),
            must_change_password=bool(row["must_change_password"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
