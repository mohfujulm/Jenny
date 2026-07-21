from __future__ import annotations

import unittest

from app.path_tags import infer_watched_path_tags


class WatchedPathTagTests(unittest.TestCase):
    def test_infers_team_workflow_tags_from_dropbox_project_path(self) -> None:
        tags = infer_watched_path_tags(
            r"\Vasquez Integrators Dropbox\01. Project Delivery\00. Projects"
            r"\43. PANYNJ - EWR Innomotics VMSS\Working Moh\Project Notes\meeting.md"
        )

        self.assertEqual(
            tags,
            [
                "workflow:project",
                "project-number:43",
                "project:PANYNJ - EWR Innomotics VMSS",
                "client:PANYNJ",
                "site:EWR",
                "owner:Moh",
                "workstream:Project Notes",
            ],
        )

    def test_uses_first_project_subfolder_as_workstream_without_owner_folder(self) -> None:
        tags = infer_watched_path_tags(
            "/Dropbox/00. Projects/12. ACME - BOS Controls/02. Submittals/register.xlsx"
        )

        self.assertIn("project-number:12", tags)
        self.assertIn("client:ACME", tags)
        self.assertIn("site:BOS", tags)
        self.assertIn("workstream:Submittals", tags)
        self.assertFalse(any(tag.startswith("owner:") for tag in tags))

    def test_does_not_guess_project_tags_without_projects_marker(self) -> None:
        tags = infer_watched_path_tags(r"C:\Dropbox\Shared\Working Moh\notes.txt")
        self.assertEqual(tags, [])


if __name__ == "__main__":
    unittest.main()
