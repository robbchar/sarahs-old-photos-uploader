"""OAuth for the Sheets API.

The Cloud project must sit inside the lcpsociety.org organization with the
consent screen's user type set to Internal. That combination needs no Google
verification review and is not subject to the 7-day refresh-token expiry that
applies to External apps in Testing status. A gmail.com account cannot
authorize an Internal app - Google returns org_internal."""
from __future__ import annotations

from pathlib import Path
from typing import cast

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_TOKEN_PATH = Path(".ignored/google-token.json")
DEFAULT_CLIENT_SECRETS_PATH = Path(".ignored/google-client-secret.json")


class AuthUnavailable(Exception):
    pass


def load_credentials(
    token_path: Path, client_secrets_path: Path, interactive: bool
) -> Credentials:
    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        _save(credentials, token_path)
        return credentials

    if not interactive:
        raise AuthUnavailable(
            "Google authorization is needed but this run is not attached to a "
            "terminal, so the browser consent flow cannot be shown. Run "
            "'python ia_bulk.py auth' from a terminal first."
        )

    if not client_secrets_path.exists():
        raise AuthUnavailable(
            f"missing OAuth client secrets at {client_secrets_path}. Download the "
            "desktop client credentials from the Google Cloud console (Internal "
            "user type) and save them there."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)
    # run_local_server's inferred return type is a union that also covers
    # workforce-identity-federation credentials; that branch only triggers
    # for a "3pi" client config, which from_client_secrets_file never
    # produces, so the runtime value is always oauth2.credentials.Credentials.
    credentials = cast(Credentials, flow.run_local_server(port=0))
    _save(credentials, token_path)
    return credentials


def _save(credentials: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
