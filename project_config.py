"""The per-project registry block.

Everything technical lives here rather than on the command line: the IA
collection, where the files are, and how a row's file path is built. A wrong
--collection on a --live run used to push real files into the wrong collection
and report success, with nothing to catch it. See docs/DECISIONS.md,
"Technical configuration lives in the registry, not the command line"."""
from __future__ import annotations

from dataclasses import dataclass

from column_map import normalize_header

REQUIRED_KEYS = (
    "mediatype",
    "ia_collection",
    "sheet_id",
    "test_sheet_id",
    "sheet_tab",
    "files_dir",
    "file_template",
)

# Which files on the drive count as photographs. Optional with a default,
# unlike required_for_upload: extensions describe what a photo file IS, while
# required_for_upload encodes editorial policy that must not be inherited by
# silence. The default excludes the contact-sheet PDFs that already sit among
# the photos under data/.
DEFAULT_PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".tif", ".tiff", ".png")


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
    required_for_upload: tuple[str, ...]
    photo_extensions: tuple[str, ...]

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
    if not isinstance(projects, dict):
        raise ConfigError(
            f"registry 'projects' must be an object mapping project ids to their "
            f"configuration, got {type(projects).__name__!r}"
        )
    if project_id not in projects:
        known = ", ".join(sorted(projects)) or "(none registered)"
        raise ConfigError(f"unknown project '{project_id}'; registry knows: {known}")

    block = projects[project_id]
    # Checked before the first block.get() below. Without this, a hand-edited
    # registry whose project value is a string or a list - a botched merge, a
    # half-finished edit - raises AttributeError: 'str' object has no attribute
    # 'get' as a bare traceback, while every other shape problem in this
    # function produces a ConfigError naming the fix. Both cmd_validate and
    # upload_from_sheet call this before any Sheet I/O, so that traceback is
    # the operator's first contact with the tool.
    if not isinstance(block, dict):
        raise ConfigError(
            f"project '{project_id}' must be an object holding its configuration keys "
            f"({', '.join(REQUIRED_KEYS)}, required_for_upload), got "
            f"{type(block).__name__!r}"
        )

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

    required_for_upload = block.get("required_for_upload")
    if required_for_upload is None:
        raise ConfigError(
            f"project '{project_id}' is missing required registry key: "
            "required_for_upload. It lists the normalized column names a human "
            "must fill in before a row can be uploaded, e.g. [\"title\", \"theme\"]. "
            "There is deliberately no default - a project inheriting another "
            "project's readiness rules by silence is worse than stating them."
        )
    if not isinstance(required_for_upload, list):
        raise ConfigError(
            f"project '{project_id}': required_for_upload must be a list, got "
            f"{type(required_for_upload).__name__!r}"
        )
    if not required_for_upload:
        raise ConfigError(
            f"project '{project_id}': required_for_upload must name at least one column"
        )
    for name in required_for_upload:
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(
                f"project '{project_id}': every required_for_upload entry must be a "
                f"non-empty string, got {name!r}"
            )
        normalized = normalize_header(name)
        if normalized != name:
            raise ConfigError(
                f"project '{project_id}': required_for_upload entry {name!r} is raw "
                f"header text, not a normalized column name - use {normalized!r}. "
                "This is the same rule file_template follows: the Sheet's headers are "
                "normalized (lowercased, punctuation dropped, spaces to underscores) "
                "before anything matches against them, so the registry must name the "
                "normalized form. Your Sheet is fine; the registry entry is not."
            )

    raw_extensions = block.get("photo_extensions")
    if raw_extensions is None:
        photo_extensions = DEFAULT_PHOTO_EXTENSIONS
    else:
        if not isinstance(raw_extensions, list):
            raise ConfigError(
                f"project '{project_id}': photo_extensions must be a list, got "
                f"{type(raw_extensions).__name__!r}"
            )
        if not raw_extensions:
            raise ConfigError(
                f"project '{project_id}': photo_extensions must name at least one "
                "extension - an empty list would exclude every file on the drive"
            )
        for entry in raw_extensions:
            if not isinstance(entry, str) or not entry.strip():
                raise ConfigError(
                    f"project '{project_id}': every photo_extensions entry must be a "
                    f"non-empty string, got {entry!r}"
                )
        photo_extensions = tuple(
            "." + entry.strip().lstrip(".").lower() for entry in raw_extensions
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
        required_for_upload=tuple(required_for_upload),
        photo_extensions=photo_extensions,
    )
