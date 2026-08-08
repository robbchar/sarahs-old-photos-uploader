"""The per-project registry block.

Everything technical lives here rather than on the command line: the IA
collection, where the files are, and how a row's file path is built. A wrong
--collection on a --live run used to push real files into the wrong collection
and report success, with nothing to catch it. See docs/DECISIONS.md,
"Technical configuration lives in the registry, not the command line"."""
from __future__ import annotations

from dataclasses import dataclass

REQUIRED_KEYS = (
    "mediatype",
    "ia_collection",
    "sheet_id",
    "test_sheet_id",
    "sheet_tab",
    "files_dir",
    "file_template",
)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    collection_key: str
    mediatype: str
    ia_collection: str
    sheet_id: str
    test_sheet_id: str
    sheet_tab: str
    files_dir: str
    file_template: str

    def sheet_id_for(self, live: bool) -> str:
        return self.sheet_id if live else self.test_sheet_id


def load_project_config(registry: dict, project_id: str) -> ProjectConfig:
    # Validate collection_key at registry root
    if "collection_key" not in registry:
        raise ConfigError("registry is missing required top-level key: collection_key")

    collection_key = registry.get("collection_key", "")
    if not isinstance(collection_key, str) or not collection_key.strip():
        raise ConfigError(
            f"registry collection_key must be a non-empty string, "
            f"got {type(collection_key).__name__!r}"
        )

    projects = registry.get("projects", {})
    if project_id not in projects:
        known = ", ".join(sorted(projects)) or "(none registered)"
        raise ConfigError(f"unknown project '{project_id}'; registry knows: {known}")

    block = projects[project_id]

    # Validate all values are strings before processing
    for key in REQUIRED_KEYS:
        value = block.get(key)
        if value is not None and not isinstance(value, str):
            raise ConfigError(
                f"project '{project_id}': {key} must be a string, "
                f"got {type(value).__name__!r}"
            )

    missing = [key for key in REQUIRED_KEYS if not (block.get(key) or "").strip()]
    if missing:
        raise ConfigError(
            f"project '{project_id}' is missing required registry keys: {', '.join(missing)}"
        )

    if block["sheet_id"].strip() == block["test_sheet_id"].strip():
        raise ConfigError(
            f"project '{project_id}': sheet_id and test_sheet_id must differ, "
            "otherwise a test run can write identifiers into the real Sheet"
        )

    return ProjectConfig(
        project_id=project_id,
        collection_key=collection_key.strip(),
        mediatype=block["mediatype"].strip(),
        ia_collection=block["ia_collection"].strip(),
        sheet_id=block["sheet_id"].strip(),
        test_sheet_id=block["test_sheet_id"].strip(),
        sheet_tab=block["sheet_tab"].strip(),
        files_dir=block["files_dir"].strip(),
        file_template=block["file_template"].strip(),
    )
