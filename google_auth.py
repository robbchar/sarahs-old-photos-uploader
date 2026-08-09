"""OAuth for the Sheets API.

The Cloud project must sit inside the lcpsociety.org organization with the
consent screen's user type set to Internal. That combination needs no Google
verification review and is not subject to the 7-day refresh-token expiry that
applies to External apps in Testing status. A gmail.com account cannot
authorize an Internal app - Google returns org_internal."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import cast

from google.auth.exceptions import RefreshError
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
        try:
            credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except (ValueError, AttributeError) as exc:
            # ValueError covers both malformed JSON (json.JSONDecodeError is
            # a ValueError subclass) and valid JSON missing the required
            # fields; AttributeError covers valid JSON of the wrong shape
            # entirely, e.g. a JSON list or string instead of an object.
            raise AuthUnavailable(
                f"the cached token at {token_path} is unreadable ({exc}). Delete "
                "it and re-run 'python ia_bulk.py auth' to re-authorize."
            ) from exc

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError:
            # The refresh token itself is dead -- revoked, or killed by an
            # org password change. Treat this exactly like having no cached
            # credentials at all so the caller can re-consent below, rather
            # than leaking a raw google.auth exception up to the caller.
            credentials = None
        else:
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
    """Write the token atomically.

    This tool runs long unattended batches, so a process killed mid-write
    must never leave a truncated token file behind -- that would surface
    later as an opaque corrupt-token failure with no clue what happened.
    Writing to a temp file in the same directory and then swapping it into
    place with os.replace() means the on-disk file is always either the old
    complete token or the new complete token, never a partial one.
    """
    token_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=token_path.parent, prefix=f".{token_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(credentials.to_json())
        os.replace(tmp_name, token_path)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise
