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
            .get(spreadsheetId=self._spreadsheet_id, range=self._tab)
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
                {"range": f"{self._tab}!{update.a1}", "values": [[update.value]]}
                for update in updates
            ],
        }
        self._service.spreadsheets().values().batchUpdate(
            spreadsheetId=self._spreadsheet_id, body=body
        ).execute()
