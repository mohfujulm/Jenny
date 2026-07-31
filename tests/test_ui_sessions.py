from __future__ import annotations

import unittest

from app.ui_sessions import BrowserSessionRegistry


class BrowserSessionRegistryTests(unittest.TestCase):
    def test_heartbeat_registers_and_remove_closes_a_session(self) -> None:
        current_time = [10.0]
        registry = BrowserSessionRegistry(
            ttl_seconds=30,
            clock=lambda: current_time[0],
        )

        self.assertEqual(registry.heartbeat("browser-tab-1"), 1)
        self.assertEqual(registry.active_count(), 1)
        self.assertEqual(registry.remove("browser-tab-1"), 0)
        self.assertEqual(registry.active_count(), 0)

    def test_multiple_tabs_are_counted_independently(self) -> None:
        registry = BrowserSessionRegistry()

        registry.heartbeat("browser-tab-1")
        registry.heartbeat("browser-tab-2")

        self.assertEqual(registry.active_count(), 2)
        self.assertEqual(registry.remove("browser-tab-1"), 1)

    def test_expired_sessions_are_pruned(self) -> None:
        current_time = [10.0]
        registry = BrowserSessionRegistry(
            ttl_seconds=30,
            clock=lambda: current_time[0],
        )
        registry.heartbeat("browser-tab-1")

        current_time[0] = 41.0

        self.assertEqual(registry.active_count(), 0)

    def test_heartbeat_refreshes_the_expiration_time(self) -> None:
        current_time = [10.0]
        registry = BrowserSessionRegistry(
            ttl_seconds=30,
            clock=lambda: current_time[0],
        )
        registry.heartbeat("browser-tab-1")
        current_time[0] = 35.0
        registry.heartbeat("browser-tab-1")
        current_time[0] = 60.0

        self.assertEqual(registry.active_count(), 1)

    def test_invalid_session_ids_are_rejected(self) -> None:
        registry = BrowserSessionRegistry()

        for session_id in ("", "contains spaces", "../path", "x" * 129):
            with self.subTest(session_id=session_id):
                with self.assertRaisesRegex(ValueError, "Invalid browser session ID"):
                    registry.heartbeat(session_id)


if __name__ == "__main__":
    unittest.main()
