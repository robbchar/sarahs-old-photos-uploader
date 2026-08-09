import pytest

from sheet_client import CellUpdate, SheetClient, column_letter


class FakeValues:
    """Records requests instead of issuing them."""

    def __init__(self, grid, get_response=None):
        self.grid = grid
        self.batch_update_calls = []
        self.get_spreadsheet_id = None
        self.batch_update_spreadsheet_id = None
        self._get_response = get_response if get_response is not None else {"values": grid}

    def get(self, spreadsheetId, range):
        self.get_spreadsheet_id = spreadsheetId
        self.get_range = range
        return _Executable(self._get_response)

    def batchUpdate(self, spreadsheetId, body):
        self.batch_update_spreadsheet_id = spreadsheetId
        self.batch_update_calls.append(body)
        return _Executable({"totalUpdatedCells": len(body["data"])})


class _Executable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeService:
    def __init__(self, grid, get_response=None):
        self.values_api = FakeValues(grid, get_response)

    def spreadsheets(self):
        return self

    def values(self):
        return self.values_api


@pytest.mark.parametrize("index,letter", [(0, "A"), (25, "Z"), (26, "AA"), (27, "AB")])
def test_column_letter(index, letter):
    assert column_letter(index) == letter


def test_read_grid_returns_rows_from_the_named_tab():
    service = FakeService([["Title"], ["Alderbrook Hall"]])
    client = SheetClient(service, "SHEET_ID", "Donor Photos")

    assert client.read_grid() == [["Title"], ["Alderbrook Hall"]]
    assert service.values_api.get_range == "Donor Photos"
    assert service.values_api.get_spreadsheet_id == "SHEET_ID"


def test_write_cells_issues_a_single_batch_request():
    service = FakeService([["Title"]])
    client = SheetClient(service, "SHEET_ID", "Donor Photos")

    client.write_cells([CellUpdate("B2", "x"), CellUpdate("B3", "y")])

    assert len(service.values_api.batch_update_calls) == 1
    body = service.values_api.batch_update_calls[0]
    assert body["data"] == [
        {"range": "Donor Photos!B2", "values": [["x"]]},
        {"range": "Donor Photos!B3", "values": [["y"]]},
    ]
    assert service.values_api.batch_update_spreadsheet_id == "SHEET_ID"


def test_write_cells_with_nothing_to_write_makes_no_request():
    service = FakeService([["Title"]])
    client = SheetClient(service, "SHEET_ID", "Donor Photos")

    client.write_cells([])

    assert service.values_api.batch_update_calls == []


def test_read_grid_with_empty_sheet_returns_empty_list():
    # Google omits the "values" key entirely for empty ranges
    service = FakeService([], get_response={})
    client = SheetClient(service, "SHEET_ID", "Donor Photos")

    result = client.read_grid()

    assert result == []
    assert service.values_api.get_spreadsheet_id == "SHEET_ID"
