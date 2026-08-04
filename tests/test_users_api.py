"""Test authentication endpoint authorization and response/cookie behavior."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException, Response
from starlette.requests import Request

from app import main
from app.models import AuthLoginRequest, AuthSignupRequest
from app.user_store import UserStore


def _request_with_cookie(cookie: str = "") -> Request:
    headers = [(b"cookie", cookie.encode("latin-1"))] if cookie else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


class UsersApiTests(unittest.TestCase):
    def test_signup_is_always_a_member_and_sets_session_cookie(self) -> None:
        with TemporaryDirectory() as directory:
            store = UserStore(Path(directory) / "application.sqlite")
            response = Response()
            with patch.object(main, "user_store", store):
                created = main.signup(
                    AuthSignupRequest(
                        username="person",
                        display_name="Person",
                        password="PortablePass1",
                    ),
                    response,
                )

            self.assertTrue(created.authenticated)
            self.assertEqual(created.user.role, "member")
            self.assertIn("askjenny_session=", response.headers["set-cookie"])
            self.assertIn("HttpOnly", response.headers["set-cookie"])

    def test_login_and_session_lookup(self) -> None:
        with TemporaryDirectory() as directory:
            store = UserStore(Path(directory) / "application.sqlite")
            user = store.create_user(
                username="person",
                display_name="Person",
                password="PortablePass1",
            )
            response = Response()
            with patch.object(main, "user_store", store):
                result = main.login(
                    AuthLoginRequest(
                        username="person",
                        password="PortablePass1",
                    ),
                    response,
                )
                cookie = response.headers["set-cookie"].split(";", 1)[0]
                session = main.get_auth_session(_request_with_cookie(cookie))

            self.assertEqual(result.user.user_id, user.user_id)
            self.assertTrue(session.authenticated)
            self.assertEqual(session.user.user_id, user.user_id)

    def test_wrong_password_returns_unauthorized(self) -> None:
        with TemporaryDirectory() as directory:
            store = UserStore(Path(directory) / "application.sqlite")
            store.create_user(
                username="person",
                display_name="Person",
                password="PortablePass1",
            )
            with patch.object(main, "user_store", store):
                with self.assertRaises(HTTPException) as context:
                    main.login(
                        AuthLoginRequest(
                            username="person",
                            password="WrongPassword1",
                        ),
                        Response(),
                    )

            self.assertEqual(context.exception.status_code, 401)

    def test_public_user_provisioning_routes_are_removed(self) -> None:
        route_paths = {
            (route.path, method)
            for route in main.app.routes
            for method in getattr(route, "methods", set())
        }
        self.assertNotIn(("/api/users", "GET"), route_paths)
        self.assertNotIn(("/api/users", "POST"), route_paths)

    def test_cancel_chat_is_scoped_to_the_signed_in_user(self) -> None:
        with TemporaryDirectory() as directory:
            store = UserStore(Path(directory) / "application.sqlite")
            user = store.create_user(
                username="person",
                display_name="Person",
                password="PortablePass1",
            )
            response = Response()
            fake_agent = Mock()
            fake_agent.cancel_request.return_value = True
            with patch.object(main, "user_store", store), patch.object(
                main,
                "agent",
                fake_agent,
            ):
                main.login(
                    AuthLoginRequest(
                        username="person",
                        password="PortablePass1",
                    ),
                    response,
                )
                cookie = response.headers["set-cookie"].split(";", 1)[0]
                result = main.cancel_chat(
                    "request-1",
                    _request_with_cookie(cookie),
                )

            self.assertTrue(result["cancelled"])
            fake_agent.cancel_request.assert_called_once_with(
                "request-1",
                user.user_id,
            )


if __name__ == "__main__":
    unittest.main()
