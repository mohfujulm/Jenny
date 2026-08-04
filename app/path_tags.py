"""Infer searchable metadata from structured watched-folder paths.

The supported path convention encodes project number, project name, site, and
working owner in directory names.  Inference is best-effort: unfamiliar paths
produce fewer tags rather than blocking file synchronization.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any


NUMBERED_FOLDER_RE = re.compile(r"^(?P<number>\d{1,4})\.\s*(?P<label>.+?)\s*$")
WORKING_OWNER_RE = re.compile(
    r"^working(?:\s*[-:–—]\s*|\s+)(?P<owner>.+?)\s*$",
    re.IGNORECASE,
)
PROJECT_SEPARATOR_RE = re.compile(r"\s+[-–—]\s+")
SITE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def infer_watched_path_tags(path: str | Path) -> list[str]:
    """Infer structured workflow tags from a Dropbox-style project path."""
    parts = _path_parts(path)
    if len(parts) < 2:
        return []

    project_marker_index = next(
        (
            index
            for index, part in enumerate(parts[:-1])
            if _strip_numbered_prefix(part)[1].lower() == "projects"
        ),
        None,
    )
    if project_marker_index is None or project_marker_index + 1 >= len(parts):
        return []

    project_segment = parts[project_marker_index + 1]
    project_number, project_name = _strip_numbered_prefix(project_segment)
    if not project_number or not project_name:
        return []

    tags = [
        "workflow:project",
        f"project-number:{project_number}",
        f"project:{project_name}",
    ]

    project_name_parts = PROJECT_SEPARATOR_RE.split(project_name, maxsplit=1)
    if len(project_name_parts) == 2:
        client = project_name_parts[0].strip()
        project_detail = project_name_parts[1].strip()
        if client:
            tags.append(f"client:{client}")
        site_match = SITE_TOKEN_RE.match(project_detail)
        if site_match:
            tags.append(f"site:{site_match.group(0)}")

    directory_parts = parts[project_marker_index + 2 : -1]
    owner: str | None = None
    owner_index: int | None = None
    for index, part in enumerate(directory_parts):
        owner_match = WORKING_OWNER_RE.match(part)
        if owner_match:
            owner = owner_match.group("owner").strip()
            owner_index = index
            break

    if owner:
        tags.append(f"owner:{owner}")

    workstream: str | None = None
    if owner_index is not None and owner_index + 1 < len(directory_parts):
        workstream = _strip_numbered_prefix(directory_parts[owner_index + 1])[1]
    if not workstream:
        workstream = next(
            (
                _strip_numbered_prefix(part)[1]
                for part in directory_parts
                if not WORKING_OWNER_RE.match(part)
                and _strip_numbered_prefix(part)[1]
            ),
            None,
        )
    if workstream:
        tags.append(f"workstream:{workstream}")

    return _deduplicate(tags)


def _path_parts(path: str | Path) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[\\/]+", str(path or "").strip())
        if part.strip() and part.strip() not in {".", ".."}
    ]


def _strip_numbered_prefix(value: Any) -> tuple[str | None, str]:
    normalized = str(value or "").strip()
    match = NUMBERED_FOLDER_RE.match(normalized)
    if not match:
        return None, normalized
    return match.group("number"), match.group("label").strip()


def _deduplicate(tags: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(tag)
    return normalized
