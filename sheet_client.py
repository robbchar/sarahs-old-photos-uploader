"""Thin transport over the Sheets API. Knows about cells and ranges; knows
nothing about identifiers, projects, or Internet Archive."""
from __future__ import annotations

from dataclasses import dataclass


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def quote_tab(tab: str) -> str:
    """A1 notation requires a sheet name to be single-quoted unless it is
    made only of letters, digits and underscores, with any embedded single
    quote doubled: `Sara's Photos` is written `'Sara''s Photos'`.

    Quoting unconditionally rather than only when it is strictly needed:
    `'Sheet1'!A1` is equally valid for a name that would not have required
    it, so there is no case to get wrong, and no predicate to keep in sync
    with Google's rules about which characters force quoting.

    Without this, a tab named with a space - which is what a Google Sheet tab
    is usually called - made every read and every write fail, and the error
    surfaced as a generic HttpError that cmd_validate reports as "check that
    'sheet_tab' names the tab exactly (case-sensitive)", sending the operator
    to re-verify the one thing that was already right."""
    return "'" + tab.replace("'", "''") + "'"


@dataclass(frozen=True)
class CellUpdate:
    a1: str
    value: str


class SheetClient:
    def __init__(self, service, spreadsheet_id: str, tab: str) -> None:
        self._service = service
        self._spreadsheet_id = spreadsheet_id
        self._tab = tab

    def read_grid(self) -> list[list[str]]:
        response = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=quote_tab(self._tab))
            .execute()
        )
        return response.get("values", [])

    def write_cells(self, updates: list[CellUpdate]) -> None:
        """One batch request per call regardless of cell count - the Sheets API
        counts a batch as a single request against the 60/minute/user quota."""
        if not updates:
            return

        body = {
            "valueInputOption": "RAW",
            "data": [
                {"range": f"{quote_tab(self._tab)}!{update.a1}", "values": [[update.value]]}
                for update in updates
            ],
        }
        self._service.spreadsheets().values().batchUpdate(
            spreadsheetId=self._spreadsheet_id, body=body
        ).execute()
