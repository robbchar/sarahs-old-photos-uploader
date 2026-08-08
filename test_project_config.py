import pytest

from project_config import ConfigError, load_project_config

REGISTRY = {
    "collection_key": "lcps",
    "projects": {
        "sarahsoldphotos": {
            "description": "Sarah's donated collection",
            "mediatype": "image",
            "ia_collection": "lcpsociety",
            "sheet_id": "REAL_SHEET",
            "test_sheet_id": "TEST_SHEET",
            "sheet_tab": "Sheet1",
            "files_dir": "D:/lcps/photos",
            "file_template": "{cd}/{file_on_array}",
        }
    },
}


def test_load_project_config_reads_the_block():
    config = load_project_config(REGISTRY, "sarahsoldphotos")

    assert config.collection_key == "lcps"
    assert config.mediatype == "image"
    assert config.file_template == "{cd}/{file_on_array}"


def test_live_and_test_runs_select_different_sheets():
    """The safety rail: there is no flag that can point a live run at the test
    Sheet, or an identifier-writing test run at the real one."""
    config = load_project_config(REGISTRY, "sarahsoldphotos")

    assert config.sheet_id_for(live=True) == "REAL_SHEET"
    assert config.sheet_id_for(live=False) == "TEST_SHEET"


def test_unknown_project_is_rejected_by_name():
    with pytest.raises(ConfigError, match="nosuchproject"):
        load_project_config(REGISTRY, "nosuchproject")


def test_missing_required_key_names_the_key_and_the_project():
    registry = {"collection_key": "lcps", "projects": {"p": {"mediatype": "image"}}}

    with pytest.raises(ConfigError, match="sheet_id"):
        load_project_config(registry, "p")


def test_identical_sheet_ids_are_rejected():
    """Pointing both at one Sheet would let a test run write identifiers into
    the real Sheet - the exact combination the two-ID design exists to prevent."""
    registry = {
        "collection_key": "lcps",
        "projects": {
            "p": {
                **REGISTRY["projects"]["sarahsoldphotos"],
                "sheet_id": "SAME",
                "test_sheet_id": "SAME",
            }
        },
    }

    with pytest.raises(ConfigError, match="must differ"):
        load_project_config(registry, "p")
