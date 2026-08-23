import json
from datetime import datetime
from pathlib import Path

import pytest
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials

import google_auth
from google_auth import (
    DEFAULT_CLIENT_SECRETS_PATH,
    DEFAULT_TOKEN_PATH,
    SCOPES,
    AuthUnavailable,
    _save,
    load_credentials,
)


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


def _write_client_secrets(path):
    """A realistic desktop-app client secrets file, as it would exist on any
    machine that has ever run through the consent flow once already."""
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "id.apps.googleusercontent.com",
                    "project_id": "lcps-archive-upload",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_secret": "secret",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )


def _refuse_browser_launch(*args, **kwargs):
    """Stand-in for InstalledAppFlow.from_client_secrets_file. Tests that
    should never reach the interactive consent flow monkeypatch it to this,
    so a regression shows up as an immediate, unambiguous AssertionError
    instead of the real method opening a browser and hanging forever
    waiting for a human who, on an unattended batch machine, isn't there."""
    raise AssertionError(
        "load_credentials launched the interactive browser consent flow when "
        "it should have reused or refreshed a cached token, or failed fast"
    )


def test_valid_cached_token_is_reused_without_a_browser(tmp_path, monkeypatch):
    token_path = tmp_path / "token.json"
    _write_token(token_path)
    monkeypatch.setattr(
        "google_auth.InstalledAppFlow.from_client_secrets_file", _refuse_browser_launch
    )

    credentials = load_credentials(token_path, tmp_path / "secrets.json", interactive=True)

    assert credentials.valid


def test_expired_token_with_valid_refresh_token_is_refreshed_without_a_browser(
    tmp_path, monkeypatch
):
    """The happy path through the refresh branch: previously untested. A
    refreshed token must be reused directly, never handed off to the
    browser flow, and the refreshed token must be re-cached to disk."""
    token_path = tmp_path / "token.json"
    _write_token(token_path, expired=True)

    def _fake_successful_refresh(self, request):
        self.token = "refreshed-access-token"
        self.expiry = datetime(2999, 1, 1)

    monkeypatch.setattr(Credentials, "refresh", _fake_successful_refresh)
    monkeypatch.setattr(
        "google_auth.InstalledAppFlow.from_client_secrets_file", _refuse_browser_launch
    )

    credentials = load_credentials(token_path, tmp_path / "secrets.json", interactive=True)

    assert credentials.token == "refreshed-access-token"
    saved = json.loads(token_path.read_text(encoding="utf-8"))
    assert saved["token"] == "refreshed-access-token"


def test_dead_refresh_token_falls_through_to_the_consent_flow_when_interactive(
    tmp_path, monkeypatch
):
    """A revoked refresh token (or one killed by an org password change)
    must not leak a raw RefreshError. When interactive, it must be treated
    like having no cached credentials at all, and the caller re-consents."""
    token_path = tmp_path / "token.json"
    _write_token(token_path, expired=True)
    secrets_path = tmp_path / "secrets.json"
    _write_client_secrets(secrets_path)

    def _dead_refresh_token(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", _dead_refresh_token)

    consent_flow_reached = {"called": False}

    class _StubFlow:
        def run_local_server(self, port):
            consent_flow_reached["called"] = True
            return Credentials(
                token="fresh-access-token",
                refresh_token="fresh-refresh-token",
                token_uri="https://oauth2.googleapis.com/token",
                client_id="id",
                client_secret="secret",
                scopes=SCOPES,
            )

    monkeypatch.setattr(
        "google_auth.InstalledAppFlow.from_client_secrets_file",
        lambda *args, **kwargs: _StubFlow(),
    )

    credentials = load_credentials(token_path, secrets_path, interactive=True)

    assert consent_flow_reached["called"]
    assert credentials.token == "fresh-access-token"


def test_dead_refresh_token_raises_auth_unavailable_when_not_interactive(tmp_path, monkeypatch):
    """Same dead refresh token, but non-interactive: must not hang and must
    not leak the raw RefreshError -- it has to fail loudly and clearly."""
    token_path = tmp_path / "token.json"
    _write_token(token_path, expired=True)

    def _dead_refresh_token(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", _dead_refresh_token)

    with pytest.raises(AuthUnavailable, match="not attached to a terminal"):
        load_credentials(token_path, tmp_path / "secrets.json", interactive=False)


def test_non_interactive_run_without_a_token_fails_loudly(tmp_path, monkeypatch):
    """The OAuth flow opens a browser and waits for a human. On a machine
    nobody is watching it must fail with a clear message rather than hang.

    The client secrets file is written for real here (as it would be on any
    machine that has ever completed the consent flow once) so this test is
    decoupled from the missing-client-secrets branch: if the interactive
    guard were ever removed, execution would reach the client secrets check,
    find it present, and proceed into the browser flow -- which must show up
    here as an immediate, unambiguous failure rather than a hang.
    """
    secrets_path = tmp_path / "secrets.json"
    _write_client_secrets(secrets_path)
    monkeypatch.setattr(
        "google_auth.InstalledAppFlow.from_client_secrets_file", _refuse_browser_launch
    )

    with pytest.raises(AuthUnavailable, match="not attached to a terminal"):
        load_credentials(tmp_path / "missing.json", secrets_path, interactive=False)


def test_missing_client_secrets_names_the_expected_path(tmp_path):
    secrets_path = tmp_path / "secrets.json"

    with pytest.raises(AuthUnavailable, match=str(secrets_path.name)):
        load_credentials(tmp_path / "missing.json", secrets_path, interactive=True)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("", id="empty_file"),
        pytest.param("{not valid json", id="malformed_json"),
        pytest.param("{}", id="valid_json_wrong_shape"),
    ],
)
def test_corrupt_cached_token_raises_an_actionable_error(tmp_path, content):
    """An empty file, malformed JSON, or valid JSON missing the required
    fields must all surface as a clear AuthUnavailable telling the operator
    what to do, not as a raw JSONDecodeError/ValueError/AttributeError."""
    token_path = tmp_path / "token.json"
    token_path.write_text(content, encoding="utf-8")

    with pytest.raises(AuthUnavailable, match="unreadable"):
        load_credentials(token_path, tmp_path / "secrets.json", interactive=True)


def test_save_is_atomic_and_never_leaves_a_partial_token_on_failure(tmp_path, monkeypatch):
    """If the process dies between writing the temp file and the atomic
    rename, the previous good token on disk must be left completely intact
    -- never truncated -- and no stray temp file should be left behind."""
    token_path = tmp_path / "token.json"
    token_path.write_text("previous-good-token-content", encoding="utf-8")

    def _simulated_crash(*args, **kwargs):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr("google_auth.os.replace", _simulated_crash)

    credentials = Credentials(
        token="new-token",
        refresh_token="refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="id",
        client_secret="secret",
        scopes=SCOPES,
    )

    with pytest.raises(OSError, match="simulated crash mid-write"):
        _save(credentials, token_path)

    assert token_path.read_text(encoding="utf-8") == "previous-good-token-content"
    assert list(tmp_path.iterdir()) == [token_path]


def test_default_paths_are_absolute_and_anchored_to_the_project_root(monkeypatch, tmp_path):
    """DEFAULT_TOKEN_PATH/DEFAULT_CLIENT_SECRETS_PATH must not depend on the
    process's current working directory. Before this, they were bare
    relative Paths (Path(".ignored/google-token.json")), which only resolve
    against whatever directory the process happens to be running from at the
    moment they're opened - so `python ia_bulk.py validate` invoked from
    anywhere other than the project root would silently miss an existing
    cached token (never reusing it, never refreshing it) and would write a
    fresh one into a wrong, unintended .ignored/ directory instead of
    failing loudly. Anchoring them to this module's own directory (which is
    also where ia_bulk.py lives) makes the invocation directory stop
    mattering."""
    monkeypatch.chdir(tmp_path)
    project_root = Path(google_auth.__file__).resolve().parent

    assert DEFAULT_TOKEN_PATH.is_absolute()
    assert DEFAULT_CLIENT_SECRETS_PATH.is_absolute()
    assert DEFAULT_TOKEN_PATH == project_root / ".ignored" / "google-token.json"
    assert DEFAULT_CLIENT_SECRETS_PATH == project_root / ".ignored" / "google-client-secret.json"


def test_unreachable_google_during_refresh_does_not_discard_the_cached_token(
    tmp_path, monkeypatch
):
    """A TransportError means Google could not be REACHED, which is a
    different thing from a dead refresh token and must not be handled as
    one. Before this, only RefreshError was caught, so a dropped connection
    mid-refresh escaped load_credentials as a raw google.auth traceback on a
    tool whose whole design is a clean stderr message instead."""
    token_path = tmp_path / "token.json"
    _write_token(token_path, expired=True)
    secrets_path = tmp_path / "secrets.json"
    _write_client_secrets(secrets_path)
    original = token_path.read_text(encoding="utf-8")

    def _unreachable(self, request):
        raise TransportError("Failed to establish a new connection")

    monkeypatch.setattr(Credentials, "refresh", _unreachable)
    monkeypatch.setattr(
        "google_auth.InstalledAppFlow.from_client_secrets_file", _refuse_browser_launch
    )

    with pytest.raises(AuthUnavailable) as exc:
        load_credentials(token_path, secrets_path, interactive=True)

    # A network problem, named as one - not "re-authorize", which would send
    # the operator to fix something that is not broken.
    assert "network problem" in str(exc.value)
    assert "nothing to re-authorize" in str(exc.value)
    # The cached token was fine; discarding it would turn a transient outage
    # into a re-consent.
    assert token_path.read_text(encoding="utf-8") == original


def test_unreachable_google_during_refresh_fails_the_same_way_when_not_interactive(
    tmp_path, monkeypatch
):
    """The unattended path must not report a network outage as "run this
    from a terminal to authorize", which is the message the non-interactive
    branch below it produces."""
    token_path = tmp_path / "token.json"
    _write_token(token_path, expired=True)

    def _unreachable(self, request):
        raise TransportError("Failed to establish a new connection")

    monkeypatch.setattr(Credentials, "refresh", _unreachable)

    with pytest.raises(AuthUnavailable) as exc:
        load_credentials(token_path, tmp_path / "secrets.json", interactive=False)

    assert "network problem" in str(exc.value)
    assert "not attached to a terminal" not in str(exc.value)
