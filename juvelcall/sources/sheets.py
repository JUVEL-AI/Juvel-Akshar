"""Google Sheets source (needs a service-account JSON shared on the sheet). Optional."""
from __future__ import annotations
from zoneinfo import ZoneInfo
from ..models import RawLead
from .csv_source import lead_from_row


def read_sheet(service_account_json: "str | dict", spreadsheet_id: str, worksheet: str, cols: dict,
               tz: ZoneInfo, source: str = "meta_sheet") -> list[RawLead]:
    import gspread
    import json as _json
    if isinstance(service_account_json, dict) or str(service_account_json).lstrip().startswith("{"):
        info = service_account_json if isinstance(service_account_json, dict) else _json.loads(service_account_json)
        gc = gspread.service_account_from_dict(info)
    else:
        gc = gspread.service_account(filename=service_account_json)
    ws = gc.open_by_key(spreadsheet_id).worksheet(worksheet)
    out = []
    for i, row in enumerate(ws.get_all_records(), start=2):
        try:
            out.append(lead_from_row(row, cols, tz, source, f"row {i}"))
        except Exception:
            continue
    return out
