"""
sheets.py — Google Sheets API integration and timetable grid parser.

Fetches the raw grid from the spreadsheet, extracts the course list from
the right-hand table (cols K–O), then iterates date rows to produce a flat
list of TimetableEvent objects — one per non-empty session cell.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, time

import openpyxl
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2.credentials import Credentials

from utils import (
    extract_year_from_metadata,
    get_session_times,
    is_month_name,
    is_special_event,
    extract_session_number,
    normalize_course_code,
    pad_rows,
    parse_embedded_time,
    reconstruct_date,
)

logger = logging.getLogger(__name__)

# Column indices (0-based) within a padded 15-element row
_COL_LABEL   = 0   # A: month name or week label (W-1, W-2 …)
_COL_DATE    = 1   # B: day-of-month number
_COL_DAY     = 2   # C: day name (Mon, Tue …)
_COL_S1      = 3   # D: Session-1  9:00–10:30
_COL_S2      = 4   # E: Session-2  10:45–12:15
_COL_S3      = 5   # F: Session-3  12:30–14:00
_COL_S4      = 6   # G: Session-4  15:00–16:30
_COL_S5      = 7   # H: Session-5  16:45–18:15
_COL_S6      = 8   # I: Session-6 (evening, no header; ~18:30–20:00)
# Cols J (index 9) is empty separator
_COL_SN      = 10  # K: serial number in course list
_COL_NAME    = 11  # L: full course name
_COL_FACULTY = 12  # M: faculty name
_COL_CODE    = 13  # N: course code
_COL_CREDIT  = 14  # O: credit value

# Slots 1–5 map to columns D–H; slot 6 maps to col I
_SLOT_COLS = {1: _COL_S1, 2: _COL_S2, 3: _COL_S3, 4: _COL_S4, 5: _COL_S5, 6: _COL_S6}

# Sheet data range — covers all data rows + course list
DEFAULT_SHEET_NAME  = "Term-I, Session Plan"
DEFAULT_DATA_RANGE  = "'Term-I, Session Plan'!A1:O89"
_DATA_START_ROW_IDX = 6  # 0-based index of the first real data row (sheet row 7)


# ---------------------------------------------------------------------------
# Drive API fetch (for Excel .xlsx files stored on Google Drive)
# ---------------------------------------------------------------------------

def fetch_excel_from_drive(
    file_id: str,
    credentials: Credentials,
) -> list[list]:
    """
    Download an Excel (.xlsx) file from Google Drive and return its rows.

    This is used when the source file is stored as an Office format on Drive
    rather than as a native Google Sheet (identified by HTTP 400 from Sheets API).
    Uses the Drive API v3 files.get_media endpoint with drive.readonly scope.

    Returns the same list[list] format as fetch_sheet_data — padded to 15 cols.
    """
    try:
        service = build("drive", "v3", credentials=credentials)
        request = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buf.seek(0)
    except HttpError as exc:
        status = exc.resp.status
        if status == 403:
            raise PermissionError(
                "Cannot download the file from Drive (HTTP 403). "
                "Make sure the file is shared with your Google account."
            ) from exc
        if status == 404:
            raise FileNotFoundError(
                "File not found on Drive (HTTP 404). Double-check the URL."
            ) from exc
        raise

    wb = openpyxl.load_workbook(buf, data_only=True)
    ws = wb.active
    rows = []
    for row in ws.iter_rows():
        row_data = []
        for cell in row:
            # Treat struck-through cells as empty — they've been cancelled
            if cell.font and cell.font.strike:
                row_data.append(None)
            else:
                row_data.append(cell.value)
        rows.append(row_data)
    logger.info("Downloaded Excel file from Drive: %d rows, sheet '%s'.", len(rows), ws.title)
    return pad_rows(rows)


@dataclass
class TimetableEvent:
    date:           date
    slot_index:     int         # 1–6
    cell_text:      str         # raw cell value (preserved for UID + description)
    course_code:    str         # normalized base code, e.g. 'OB', 'MOC', 'ETB'
    course_name:    str         # full name from course map; cell_text if unmapped
    faculty:        str         # from course map; '' if unmapped
    session_number: str         # '1', 'S7', 'Quiz-1', 'Exam', 'Workshop', …
    start_time:     time = field(repr=False)
    end_time:       time = field(repr=False)
    is_special:     bool = False
    location:       str  = "CR-7C-15"

    def __str__(self) -> str:
        return (
            f"{self.date.isoformat()} S{self.slot_index} "
            f"{self.start_time:%H:%M}–{self.end_time:%H:%M}  "
            f"{self.course_code:<8}  {self.cell_text}"
        )


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------

def fetch_sheet_data(
    spreadsheet_id: str,
    credentials: Credentials,
    sheet_range: str = DEFAULT_DATA_RANGE,
) -> list[list]:
    """
    Fetch raw cell values from the Google Sheets API.

    Returns a list of rows (each row is a list of strings).  Trailing empty
    cells are omitted by the API, so each row is padded to 15 elements.

    Raises:
        HttpError 403 — sheet not accessible (wrong account / no sharing).
        HttpError 404 — spreadsheet not found (wrong ID).
    """
    try:
        service = build("sheets", "v4", credentials=credentials)
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=sheet_range)
            .execute()
        )
    except HttpError as exc:
        status = exc.resp.status
        if status == 403:
            raise PermissionError(
                "Cannot access the spreadsheet (HTTP 403). "
                "Make sure the sheet is shared with your Google account, "
                "or that you signed in with the correct account."
            ) from exc
        if status == 404:
            raise FileNotFoundError(
                "Spreadsheet not found (HTTP 404). "
                "Double-check the --sheet-url value."
            ) from exc
        raise

    rows = result.get("values", [])
    if not rows:
        raise ValueError(
            f"The Sheets API returned no data for range '{sheet_range}'. "
            "Check the sheet tab name with --sheet-name."
        )

    logger.debug("Fetched %d rows from range '%s'.", len(rows), sheet_range)
    return pad_rows(rows)


# ---------------------------------------------------------------------------
# Course map parser
# ---------------------------------------------------------------------------

def parse_course_map(rows: list[list]) -> dict[str, dict]:
    """
    Build a mapping from normalized course code → course info dict.

    Reads rows 2–13 (0-indexed 1–12), columns K–O (indices 10–14).
    Keys are upper-cased codes.  'MoC' is stored as 'MOC'.

    Returns:
        {
            'OB':    {'name': 'Organizational Behaviour', 'faculty': 'Prof. Dina Banerjee',
                      'code': 'OB', 'credit': 2.0},
            'MOC':   {'name': 'Managerial Oral Communication', ...},
            ...
        }
    """
    course_map: dict[str, dict] = {}

    for row_idx in range(1, min(13, len(rows))):
        row = rows[row_idx]
        sn_cell   = row[_COL_SN]
        name_cell = row[_COL_NAME]
        fac_cell  = row[_COL_FACULTY]
        code_cell = row[_COL_CODE]
        cred_cell = row[_COL_CREDIT]

        # Skip header row and totals row (S.N. must be numeric)
        if sn_cell is None:
            continue
        try:
            float(str(sn_cell))
        except (ValueError, TypeError):
            continue

        if not code_cell:
            continue

        raw_code = str(code_cell).strip()
        key = raw_code.upper()
        # Normalize MoC → MOC
        if key == 'MOC' or raw_code == 'MoC':
            key = 'MOC'

        credit = None
        if cred_cell is not None:
            try:
                credit = float(str(cred_cell).replace('=sum(O3:O13)', '').strip() or '0')
            except ValueError:
                credit = None

        course_map[key] = {
            'name':    str(name_cell).strip() if name_cell else raw_code,
            'faculty': str(fac_cell).strip()  if fac_cell  else '',
            'code':    raw_code,
            'credit':  credit,
        }
        logger.debug("Course map entry: %s → %s", key, course_map[key]['name'])

    logger.info("Loaded %d courses from sheet.", len(course_map))
    return course_map


# ---------------------------------------------------------------------------
# Grid parser
# ---------------------------------------------------------------------------

def parse_calendar_grid(
    rows: list[list],
    course_map: dict[str, dict],
    year: int | None = None,
    location: str = "CR-7C-15",
) -> list[TimetableEvent]:
    """
    Parse the calendar-grid section of the sheet into TimetableEvent objects.

    Grid structure (0-based column indices):
      0  = A: month name or week label (W-1…)
      1  = B: day-of-month number
      2  = C: day name (Mon…Sun)
      3–7 = D–H: Sessions 1–5
      8   = I:  Session 6 (occasional evening slot)

    Month carry-forward: col A is only populated when the month changes.
    Week labels (W-N), None, and stray characters are ignored for month tracking.
    """
    if year is None:
        year = extract_year_from_metadata(rows)

    events: list[TimetableEvent] = []
    current_month: str | None = None

    for row_idx in range(_DATA_START_ROW_IDX, len(rows)):
        row = rows[row_idx]
        col_a = row[_COL_LABEL]

        # Update current month only on recognized month names
        if col_a is not None and is_month_name(str(col_a)):
            current_month = str(col_a).strip()
            logger.debug("Row %d: month updated to '%s'", row_idx + 1, current_month)

        # Must have a day number and a known month to build a date
        day_cell = row[_COL_DATE]
        if day_cell is None or current_month is None:
            continue

        try:
            day_num = int(float(str(day_cell)))
        except (ValueError, TypeError):
            continue

        try:
            event_date = reconstruct_date(day_num, current_month, year)
        except ValueError as exc:
            logger.warning("Row %d: skipping — %s", row_idx + 1, exc)
            continue

        # Iterate session slots
        for slot_index, col_idx in _SLOT_COLS.items():
            cell = row[col_idx]
            if cell is None:
                continue
            cell_str = str(cell).strip()
            if not cell_str:
                continue

            # Determine start/end times — embedded override beats slot default
            embedded = parse_embedded_time(cell_str)
            if embedded:
                start_t, end_t = embedded
                logger.debug(
                    "Row %d slot %d: embedded time %s–%s in '%s'",
                    row_idx + 1, slot_index, start_t, end_t, cell_str,
                )
            else:
                start_t, end_t = get_session_times(slot_index)

            code   = normalize_course_code(cell_str)
            info   = course_map.get(code.upper())
            if info is None:
                # Try partial match (e.g. code fragment in key)
                info = _fuzzy_lookup(code, course_map)
                if info:
                    logger.debug("Fuzzy match: '%s' → '%s'", code, info['code'])
                else:
                    logger.warning(
                        "Row %d slot %d: unrecognized course code '%s' in '%s'.",
                        row_idx + 1, slot_index, code, cell_str,
                    )

            event = TimetableEvent(
                date=event_date,
                slot_index=slot_index,
                cell_text=cell_str,
                course_code=code,
                course_name=info['name']    if info else cell_str,
                faculty=info['faculty']     if info else '',
                session_number=extract_session_number(cell_str),
                start_time=start_t,
                end_time=end_t,
                is_special=is_special_event(cell_str),
                location=location,
            )
            events.append(event)
            logger.debug("Event: %s", event)

    logger.info("Parsed %d events from %d sheet rows.", len(events), len(rows) - _DATA_START_ROW_IDX)
    return events


def _fuzzy_lookup(code: str, course_map: dict[str, dict]) -> dict | None:
    """Attempt a case-insensitive prefix match when exact lookup fails."""
    code_upper = code.upper()
    for key, info in course_map.items():
        if key.startswith(code_upper) or code_upper.startswith(key):
            return info
    return None
