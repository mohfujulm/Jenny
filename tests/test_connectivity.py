"""Test network-probe classification for success, HTTP errors, and outages."""

from __future__ import annotations

from unittest.mock import patch
from urllib.error import HTTPError, URLError
import unittest

from app.connectivity import OPENAI_NETWORK_CHECK_URL, check_openai_network_access


class ConnectivityTests(unittest.TestCase):
    def test_unauthorized_response_confirms_network_access(self) -> None:
        error = HTTPError(
            OPENAI_NETWORK_CHECK_URL,
            401,
            "Unauthorized",
            hdrs=None,
            fp=None,
        )
        with patch("app.connectivity.urlopen", side_effect=error):
            status = check_openai_network_access()

        self.assertTrue(status["reachable"])
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["status_code"], 401)

    def test_connection_failure_returns_alert_status(self) -> None:
        with patch(
            "app.connectivity.urlopen",
            side_effect=URLError("network is unreachable"),
        ):
            status = check_openai_network_access()

        self.assertFalse(status["reachable"])
        self.assertEqual(status["status"], "alert")
        self.assertIn("network is unreachable", status["detail"])


if __name__ == "__main__":
    unittest.main()
