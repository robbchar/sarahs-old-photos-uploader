import json
from pathlib import Path

import pytest

from project_config import REQUIRED_KEYS, ConfigError, load_project_config

REGISTRY = {
    "collection_key": "lcps",
    "projects": {
        "sarasoldphotos": {
            "description": "Sara's donated collection",
            "mediatype": "image",
            "ia_collection": "lcpsociety",
            "sheet_id": "REAL_SHEET",
            "test_sheet_id": "TEST_SHEET",
            "sheet_tab": "Sheet1",
            "files_dir": "D:/lcps/photos",
            "file_template": "{cd}/{file_on_array}",
            "required_for_upload": ["title"],
        }
    },
}


def _registry(**overrides):
    block = {
        "mediatype": "image",
        "ia_collection": "lcpsdigitalcollection",
        "sheet_id": "real",
        "test_sheet_id": "test",
        "sheet_tab": "Sheet1",
        "files_dir": "./data",
        "file_template": "{folder}/{name}",
        "required_for_upload": ["title", "theme"],
    }
    block.update(overrides)
    return {"collection_key": "lcps", "projects": {"p": block}}


def test_load_project_config_reads_the_block():
    config = load_project_config(REGISTRY, "sarasoldphotos")

    assert config.collection_key == "lcps"
    assert config.mediatype == "image"
    assert config.file_template == "{cd}/{file_on_array}"


def test_live_and_test_runs_select_different_sheets():
    """The safety rail: there is no flag that can point a live run at the test
    Sheet, or an identifier-writing test run at the real one."""
    config = load_project_config(REGISTRY, "sarasoldphotos")

    assert config.sheet_id_for(live=True) == "REAL_SHEET"
    assert config.sheet_id_for(live=False) == "TEST_SHEET"


def test_unknown_project_is_rejected_by_name():
    with pytest.raises(ConfigError, match="nosuchproject"):
        load_project_config(REGISTRY, "nosuchproject")


def test_missing_required_key_names_the_key_and_the_project():
    registry = {"collection_key": "lcps", "projects": {"p": {"mediatype": "image"}}}

    with pytest.raises(ConfigError, match="'p'.*sheet_id"):
        load_project_config(registry, "p")


def test_identical_sheet_ids_are_rejected():
    """Pointing both at one Sheet would let a test run write identifiers into
    the real Sheet - the exact combination the two-ID design exists to prevent."""
    registry = {
        "collection_key": "lcps",
        "projects": {
            "p": {
                **REGISTRY["projects"]["sarasoldphotos"],
                "sheet_id": "SAME",
                "test_sheet_id": "SAME",
            }
        },
    }

    with pytest.raises(ConfigError, match="must differ"):
        load_project_config(registry, "p")


def test_missing_collection_key_is_rejected():
    """collection_key is a top-level registry field, not in a project block.
    Missing it should raise ConfigError, not KeyError."""
    registry = {"projects": {"p": REGISTRY["projects"]["sarasoldphotos"]}}

    with pytest.raises(ConfigError, match="collection_key"):
        load_project_config(registry, "p")


def test_non_string_collection_key_is_rejected():
    """collection_key must be a string. Non-string values should be caught
    as ConfigError, not silently accepted."""
    registry = {
        "collection_key": 123,
        "projects": {"p": REGISTRY["projects"]["sarasoldphotos"]},
    }

    with pytest.raises(ConfigError, match="collection_key.*string"):
        load_project_config(registry, "p")


def test_empty_string_collection_key_is_rejected():
    """collection_key must be a non-empty string."""
    registry = {
        "collection_key": "   ",
        "projects": {"p": REGISTRY["projects"]["sarasoldphotos"]},
    }

    with pytest.raises(ConfigError, match="collection_key"):
        load_project_config(registry, "p")


def test_non_string_project_value_is_rejected():
    """Project block values must be strings. Non-string values should raise
    ConfigError before reaching .strip()."""
    registry = {
        "collection_key": "lcps",
        "projects": {
            "p": {
                **REGISTRY["projects"]["sarasoldphotos"],
                "mediatype": 42,
            }
        },
    }

    with pytest.raises(ConfigError, match="mediatype.*string"):
        load_project_config(registry, "p")


def test_shipped_registry_json_loads_against_the_current_required_keys():
    """Pin the registry file and REQUIRED_KEYS together so the two cannot drift
    apart silently: adding a required key without adding it to the shipped
    registry, or removing a key from the registry, both fail here.

    Deliberately asserts nothing about the *values*. Those are operational
    settings that get filled in with real Sheet IDs and collection names as the
    project is configured, so asserting placeholders would make this test fail
    the moment the tool starts being used for real."""
    registry_path = Path(__file__).parent / "projects_registry.json"
    with open(registry_path) as f:
        registry = json.load(f)

    config = load_project_config(registry, "sarasoldphotos")

    for key in REQUIRED_KEYS:
        assert getattr(config, key), f"shipped registry has an empty '{key}'"


def test_required_for_upload_is_loaded_as_a_tuple():
    config = load_project_config(_registry(), "p")
    assert config.required_for_upload == ("title", "theme")


def test_missing_required_for_upload_is_an_error():
    registry = _registry()
    del registry["projects"]["p"]["required_for_upload"]
    with pytest.raises(ConfigError) as exc:
        load_project_config(registry, "p")
    assert str(exc.value) == (
        "project 'p' is missing required registry key: required_for_upload. "
        "It lists the normalized column names a human must fill in before a "
        "row can be uploaded, e.g. [\"title\", \"theme\"]. There is "
        "deliberately no default - a project inheriting another project's "
        "readiness rules by silence is worse than stating them."
    )


def test_empty_required_for_upload_is_an_error():
    with pytest.raises(ConfigError) as exc:
        load_project_config(_registry(required_for_upload=[]), "p")
    assert str(exc.value) == "project 'p': required_for_upload must name at least one column"


def test_required_for_upload_must_be_a_list_not_a_string():
    with pytest.raises(ConfigError) as exc:
        load_project_config(_registry(required_for_upload="title"), "p")
    assert str(exc.value) == "project 'p': required_for_upload must be a list, got 'str'"


def test_required_for_upload_entry_must_be_a_string():
    """Covers the per-entry isinstance branch (project_config.py's
    `not isinstance(name, str) or not name.strip()` check), which the
    original five tests never exercised: every entry passed to
    load_project_config was already a valid string."""
    with pytest.raises(ConfigError) as exc:
        load_project_config(_registry(required_for_upload=["title", 123]), "p")
    assert str(exc.value) == (
        "project 'p': every required_for_upload entry must be a non-empty "
        "string, got 123"
    )


def test_required_for_upload_entry_must_not_be_blank():
    """The other half of the same branch: a whitespace-only entry is still
    truthy, so it takes the .strip() check specifically, not just a
    non-string check, to reject it."""
    with pytest.raises(ConfigError) as exc:
        load_project_config(_registry(required_for_upload=["title", "   "]), "p")
    assert str(exc.value) == (
        "project 'p': every required_for_upload entry must be a non-empty "
        "string, got '   '"
    )


def test_raw_header_text_is_rejected_with_the_normalization_rule_explained():
    """The file_template lesson: an un-normalized name produced an error that
    read as 'your Sheet is wrong' when the Sheet was fine."""
    with pytest.raises(ConfigError) as exc:
        load_project_config(_registry(required_for_upload=["Architectura Style"]), "p")
    assert str(exc.value) == (
        "project 'p': required_for_upload entry 'Architectura Style' is raw "
        "header text, not a normalized column name - use 'architectura_style'. "
        "This is the same rule file_template follows: the Sheet's headers are "
        "normalized (lowercased, punctuation dropped, spaces to underscores) "
        "before anything matches against them, so the registry must name the "
        "normalized form. Your Sheet is fine; the registry entry is not."
    )


@pytest.mark.parametrize("block", ["see other file", ["mediatype"], 42, None])
def test_a_project_block_that_is_not_an_object_is_a_config_error_not_a_traceback(block):
    """A hand-edited registry whose project value is a string or a list - a
    botched merge, a half-finished edit - used to reach block.get() and raise
    AttributeError as a bare traceback, while every neighbouring shape
    problem produces a ConfigError naming the fix. Both cmd_validate and
    upload_from_sheet call this before any Sheet I/O, so that traceback was
    the operator's first contact with the tool."""
    registry = {"collection_key": "lcps", "projects": {"p": block}}

    with pytest.raises(ConfigError) as exc:
        load_project_config(registry, "p")

    assert "project 'p' must be an object" in str(exc.value)
    assert type(block).__name__ in str(exc.value)


def test_a_projects_value_that_is_not_an_object_is_a_config_error():
    registry = {"collection_key": "lcps", "projects": ["p"]}

    with pytest.raises(ConfigError) as exc:
        load_project_config(registry, "p")

    assert "'projects' must be an object" in str(exc.value)
