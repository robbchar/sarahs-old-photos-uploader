import json

import pytest

from google_auth import AuthUnavailable, load_credentials


def _write_token(path, expired=False):
    path.write_text(
        json.dumps(
            {
                "token": "access-token",
                "refresh_token": "refresh-token",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "id",
                "client_secret": "secret",
                "scopes": ["https://www.googleapis.com/auth/spreadsheets"],
                "expiry": "2000-01-01T00:00:00" if expired else "2999-01-01T00:00:00",
            }
        ),
        encoding="utf-8",
    )


def test_valid_cached_token_is_reused_without_a_browser(tmp_path, monkeypatch):
    token_path = tmp_path / "token.json"
    _write_token(token_path)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError(
            "load_credentials launched the interactive browser flow instead of "
            "reusing the valid cached token"
        )

    monkeypatch.setattr(
        "google_auth.InstalledAppFlow.from_client_secrets_file", _fail_if_called
    )

    credentials = load_credentials(token_path, tmp_path / "secrets.json", interactive=True)

    assert credentials.valid


def test_non_interactive_run_without_a_token_fails_loudly(tmp_path):
    """The OAuth flow opens a browser and waits for a human. On a machine
    nobody is watching it must fail with a clear message rather than hang."""
    with pytest.raises(AuthUnavailable, match="not attached to a terminal"):
        load_credentials(tmp_path / "missing.json", tmp_path / "secrets.json", interactive=False)


def test_missing_client_secrets_names_the_expected_path(tmp_path):
    secrets_path = tmp_path / "secrets.json"

    with pytest.raises(AuthUnavailable, match=str(secrets_path.name)):
        load_credentials(tmp_path / "missing.json", secrets_path, interactive=True)
