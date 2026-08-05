"""Verify shared-library data and mutations are protected at the HTTP boundary."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.datastore import DocumentLibraryRecord, FolderRecord
from app.ingestion import UploadOutcome
from app.user_store import UserStore
from tests.main_runtime import main


AUTHENTICATED_ROUTES = {
    ("GET", "/api/documents"),
    ("GET", "/api/documents/{document_id}"),
    ("POST", "/api/documents/generate"),
}

LIBRARY_MANAGER_ROUTES = {
    ("POST", "/api/local-folders/browse"),
    ("GET", "/api/watch-folders"),
    ("POST", "/api/watch-folders"),
    ("PATCH", "/api/watch-folders/{watch_id}"),
    ("POST", "/api/watch-folders/sync"),
    ("POST", "/api/watch-folders/{watch_id}/sync"),
    ("POST", "/api/watch-folders/{watch_id}/open-source"),
    ("DELETE", "/api/watch-folders/{watch_id}"),
    ("POST", "/api/documents/upload"),
    ("POST", "/api/documents/upload-batch"),
    ("POST", "/api/documents/delete"),
    ("POST", "/api/documents/delete/stream"),
    ("POST", "/api/documents/tags"),
    ("POST", "/api/documents/metadata"),
    ("POST", "/api/folders/create"),
    ("POST", "/api/folders/delete"),
    ("POST", "/api/folders/move"),
    ("POST", "/api/folders/rename"),
}


def _route_dependency_calls(method: str, path: str) -> set[object]:
    for route in main.app.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in route.methods
        ):
            return {
                dependency.call
                for dependency in route.dependant.dependencies
            }
    raise AssertionError(f"Route not found: {method} {path}")


class LibraryRouteAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        root = Path(self._temporary_directory.name)
        self.user_store = UserStore(root / "application.sqlite")
        self.member = self.user_store.create_user(
            username="member",
            display_name="Member",
            password="PortablePass1",
            role="member",
        )
        self.manager = self.user_store.create_user(
            username="manager",
            display_name="Library Manager",
            password="PortablePass1",
            role="library_manager",
        )
        self.administrator = self.user_store.create_user(
            username="administrator",
            display_name="Administrator",
            password="PortablePass1",
            role="admin",
        )
        self.member_token = self.user_store.create_session(self.member.user_id, 24)
        self.manager_token = self.user_store.create_session(self.manager.user_id, 24)
        self.administrator_token = self.user_store.create_session(
            self.administrator.user_id,
            24,
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _client(self, token: str | None = None) -> TestClient:
        client = TestClient(main.app)
        if token:
            client.cookies.set(main.AUTH_COOKIE_NAME, token)
        return client

    def test_every_library_route_declares_the_expected_guard(self) -> None:
        for method, path in AUTHENTICATED_ROUTES:
            self.assertIn(
                main._require_authenticated_user,
                _route_dependency_calls(method, path),
                f"Missing signed-in-user guard on {method} {path}",
            )

        for method, path in LIBRARY_MANAGER_ROUTES:
            self.assertIn(
                main._require_library_manager,
                _route_dependency_calls(method, path),
                f"Missing library-manager guard on {method} {path}",
            )

    def test_anonymous_document_listing_is_rejected_before_store_access(self) -> None:
        document_store = Mock()
        with (
            patch.object(main, "user_store", self.user_store),
            patch.object(main, "document_store", document_store),
        ):
            client = self._client()
            try:
                response = client.get("/api/documents")
            finally:
                client.close()

        self.assertEqual(response.status_code, 401)
        document_store.list_documents.assert_not_called()

    def test_signed_in_member_can_read_the_shared_library(self) -> None:
        document_store = Mock()
        document_store.list_documents.return_value = DocumentLibraryRecord(
            backend="semantic",
            total_documents=0,
            total_chunks=0,
            folders=[],
            documents=[],
        )
        with (
            patch.object(main, "user_store", self.user_store),
            patch.object(main, "document_store", document_store),
        ):
            client = self._client(self.member_token)
            try:
                response = client.get("/api/documents")
            finally:
                client.close()

        self.assertEqual(response.status_code, 200)
        document_store.list_documents.assert_called_once_with()

    def test_member_receives_folder_aliases_without_watched_source_details(self) -> None:
        document_store = Mock()
        document_store.list_documents.return_value = DocumentLibraryRecord(
            backend="semantic",
            total_documents=0,
            total_chunks=0,
            folders=[FolderRecord(folder_id="shared/project", display_name="project", document_count=0)],
            documents=[],
        )
        watch_service = Mock()
        watch_service.list_watchers.return_value = [
            {
                "library_folder": "shared/project",
                "alias": "PANYNJ EWR",
                "display_name": "Project Notes",
                "source_path": r"C:\\private\\Dropbox\\project",
                "root_path": r"C:\\private\\Dropbox",
            }
        ]
        with (
            patch.object(main, "user_store", self.user_store),
            patch.object(main, "document_store", document_store),
            patch.object(main, "watch_folder_service", watch_service),
        ):
            client = self._client(self.member_token)
            try:
                response = client.get("/api/documents")
            finally:
                client.close()

        self.assertEqual(response.status_code, 200)
        folder = next(
            item
            for item in response.json()["folders"]
            if item["folder_id"] == "shared/project"
        )
        self.assertEqual(folder["aliases"], ["PANYNJ EWR", "Project Notes"])
        self.assertNotIn("source_path", folder)
        self.assertNotIn("root_path", folder)

    def test_member_cannot_list_watched_source_paths(self) -> None:
        watch_service = Mock()
        with (
            patch.object(main, "user_store", self.user_store),
            patch.object(main, "watch_folder_service", watch_service),
        ):
            client = self._client(self.member_token)
            try:
                response = client.get("/api/watch-folders")
            finally:
                client.close()

        self.assertEqual(response.status_code, 403)
        watch_service.list_watchers.assert_not_called()

    def test_member_cannot_upload_or_trigger_ingestion(self) -> None:
        ingestion_service = Mock()
        with (
            patch.object(main, "user_store", self.user_store),
            patch.object(main, "ingestion_service", ingestion_service),
        ):
            client = self._client(self.member_token)
            try:
                response = client.post(
                    "/api/documents/upload",
                    json={"filename": "private.txt", "content_text": "Private data"},
                )
            finally:
                client.close()

        self.assertEqual(response.status_code, 403)
        ingestion_service.ingest_upload.assert_not_called()

    def test_library_manager_and_administrator_can_access_protected_services(self) -> None:
        watch_service = Mock()
        watch_service.list_watchers.return_value = []
        ingestion_service = Mock()
        ingestion_service.ingest_upload.return_value = UploadOutcome(
            uploaded_documents=[],
            semantic_index_rebuilt=False,
            message="No changes.",
        )
        with (
            patch.object(main, "user_store", self.user_store),
            patch.object(main, "watch_folder_service", watch_service),
            patch.object(main, "ingestion_service", ingestion_service),
        ):
            client = self._client(self.manager_token)
            try:
                watch_response = client.get("/api/watch-folders")
                upload_response = client.post(
                    "/api/documents/upload",
                    json={"filename": "allowed.txt", "content_text": "Allowed data"},
                )
            finally:
                client.close()
            administrator_client = self._client(self.administrator_token)
            try:
                administrator_response = administrator_client.get("/api/watch-folders")
            finally:
                administrator_client.close()

        self.assertEqual(watch_response.status_code, 200)
        self.assertEqual(upload_response.status_code, 200)
        self.assertEqual(administrator_response.status_code, 200)
        self.assertEqual(watch_service.list_watchers.call_count, 2)
        ingestion_service.ingest_upload.assert_called_once()


if __name__ == "__main__":
    unittest.main()
