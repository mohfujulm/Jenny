"""Verify routine HTTP authentication, role, and cross-site protections."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.user_store import UserStore
from tests.main_runtime import main


def _dependency_calls(method: str, path: str) -> set[object]:
    for route in main.app.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return {dependency.call for dependency in route.dependant.dependencies}
    raise AssertionError(f"Route not found: {method} {path}")


class RoutineApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.owner = TemporaryDirectory(prefix="routine-api-")
        self.users = UserStore(Path(self.owner.name) / "application.sqlite")
        self.member = self.users.create_user(
            username="routine-member",
            display_name="Routine Member",
            password="PortablePass1",
            role="member",
        )
        self.admin = self.users.create_user(
            username="routine-admin",
            display_name="Routine Admin",
            password="PortablePass1",
            role="admin",
        )
        self.member_token = self.users.create_session(self.member.user_id, 24)
        self.admin_token = self.users.create_session(self.admin.user_id, 24)

    def tearDown(self) -> None:
        self.owner.cleanup()

    def _client(self, token: str | None = None) -> TestClient:
        client = TestClient(main.app)
        if token:
            client.cookies.set(main.AUTH_COOKIE_NAME, token)
        return client

    def test_all_routine_routes_declare_authentication(self) -> None:
        for method, path in {
            ("GET", "/api/routines"),
            ("POST", "/api/routines"),
            ("PUT", "/api/routines/{routine_id}"),
            ("POST", "/api/routines/{routine_id}/enabled"),
            ("DELETE", "/api/routines/{routine_id}"),
            ("POST", "/api/routines/{routine_id}/run"),
            ("DELETE", "/api/routines/runs/{run_id}"),
            ("GET", "/api/routines/runs/{run_id}/document"),
        }:
            self.assertIn(main._require_authenticated_user, _dependency_calls(method, path))
        self.assertIn(
            main._require_administrator,
            _dependency_calls("POST", "/api/routines/admin/pause"),
        )

    def test_anonymous_dashboard_is_rejected_before_service_access(self) -> None:
        service = Mock()
        with patch.object(main, "user_store", self.users), patch.object(
            main, "routine_service", service
        ):
            client = self._client()
            try:
                response = client.get("/api/routines")
            finally:
                client.close()
        self.assertEqual(response.status_code, 401)
        service.dashboard.assert_not_called()

    def test_dashboard_is_scoped_to_authenticated_owner(self) -> None:
        service = Mock()
        service.dashboard.return_value = {
            "routines": [],
            "runs": [],
            "system_paused": False,
            "policy": {"enabled": True},
        }
        with patch.object(main, "user_store", self.users), patch.object(
            main, "routine_service", service
        ):
            client = self._client(self.member_token)
            try:
                response = client.get("/api/routines")
            finally:
                client.close()
        self.assertEqual(response.status_code, 200)
        service.dashboard.assert_called_once_with(self.member.user_id)

    def test_member_cannot_use_global_pause(self) -> None:
        service = Mock()
        with patch.object(main, "user_store", self.users), patch.object(
            main, "routine_service", service
        ):
            client = self._client(self.member_token)
            try:
                response = client.post("/api/routines/admin/pause", json={"paused": True})
            finally:
                client.close()
        self.assertEqual(response.status_code, 403)
        service.store.set_system_paused.assert_not_called()

    def test_cross_site_mutation_is_rejected_before_execution(self) -> None:
        service = Mock()
        with patch.object(main, "user_store", self.users), patch.object(
            main, "routine_service", service
        ):
            client = self._client(self.member_token)
            try:
                response = client.post(
                    "/api/routines/routine-1/run",
                    headers={"Origin": "https://attacker.example"},
                )
            finally:
                client.close()
        self.assertEqual(response.status_code, 403)
        service.run_now.assert_not_called()

    def test_administrator_can_pause_all_new_runs(self) -> None:
        service = Mock()
        with patch.object(main, "user_store", self.users), patch.object(
            main, "routine_service", service
        ):
            client = self._client(self.admin_token)
            try:
                response = client.post("/api/routines/admin/pause", json={"paused": True})
            finally:
                client.close()
        self.assertEqual(response.status_code, 200)
        service.store.set_system_paused.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
