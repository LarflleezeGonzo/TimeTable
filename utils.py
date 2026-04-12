"""
utils.py — Pure helper functions: session times, course code normalization,
date reconstruction, embedded time parsing, and URL parsing.
No external I/O; all functions are testable in isolation.
"""

from __future__ import annotations

import re
import logging
from datetime import date, time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session time lookup
# ---------------------------------------------------------------------------

SESSION_TIMES: dict[int, tuple[int, int, int, int]] = {
    1: (9,  0,  10, 30),   # 9:00 – 10:30
    2: (10, 45, 12, 15),   # 10:45 – 12:15
    3: (12, 30, 14,  0),   # 12:30 – 14:00
    4: (15,  0, 16, 30),   # 15:00 – 16:30
    5: (16, 45, 18, 15),   # 16:45 – 18:15
    6: (18, 30, 20,  0),   # 18:30 – 20:00  (evening / extra session)
}


def get_session_times(slot_index: int) -> tuple[time, time]:
    """Return (start_time, end_time) for a 1-based slot index."""
    if slot_index not in SESSION_TIMES:
        raise ValueError(f"Unknown slot index: {slot_index}. Valid range: 1–6.")
    sh, sm, eh, em = SESSION_TIMES[slot_index]
    return time(sh, sm), time(eh, em)


# ---------------------------------------------------------------------------
# Embedded time parsing
# ---------------------------------------------------------------------------

# Matches patterns like:
#   (6.30 - 8.00pm)          MOC evening sessions
#   02:30 - 05:30 pm         Registration row
#   (10.00-11.30am, Online)  Guest sessions
#   (10:00am - 01:00pm)      Workshops
_TIME_RE = re.compile(
    r'[\(\s](\d{1,2}[.:]\d{2})\s*(am|pm)?\s*[-–]\s*(\d{1,2}[.:]\d{2})\s*(am|pm)',
    re.IGNORECASE,
)


def _parse_time_str(t_str: str, meridiem: str | None) -> time:
    """Convert '6.30' or '10:00' + optional meridiem → time object."""
    normalized = t_str.replace('.', ':')
    parts = normalized.split(':')
    h, m = int(parts[0]), int(parts[1])
    if meridiem:
        mer = meridiem.lower()
        if mer == 'pm' and h != 12:
            h += 12
        elif mer == 'am' and h == 12:
            h = 0
    return time(h, m)


def parse_embedded_time(cell_text: str) -> tuple[time, time] | None:
    """
    Extract a custom time range embedded in a cell string.

    Returns (start_time, end_time) or None if no time pattern found.
    Handles edge cases like '02:30 - 05:30 pm' where the leading zero
    looks like AM but context implies PM.
    """
    match = _TIME_RE.search(cell_text)
    if not match:
        return None

    start_str, start_mer, end_str, end_mer = match.groups()
    end_mer = end_mer.lower()  # always present per regex

    # Infer start meridiem when absent
    if start_mer is None:
        start_h_raw = int(start_str.replace('.', ':').split(':')[0])
        end_h_raw   = int(end_str.replace('.', ':').split(':')[0])
        end_abs = end_h_raw + 12 if (end_mer == 'pm' and end_h_raw != 12) else end_h_raw
        if end_abs >= 12 and start_h_raw < 12:
            start_mer = 'am'
        else:
            start_mer = end_mer

    start_time = _parse_time_str(start_str, start_mer)
    end_time   = _parse_time_str(end_str,   end_mer)

    # Edge case: "02:30 - 05:30 pm" — start parsed as 02:30 AM but is really PM
    # If end is ≥ 12:00 and start < 08:00, the leading zero was misleading.
    if end_time.hour >= 12 and start_time.hour < 8:
        start_time = time(start_time.hour + 12, start_time.minute)

    return start_time, end_time


# ---------------------------------------------------------------------------
# Month name map
# ---------------------------------------------------------------------------

MONTH_NAME_MAP: dict[str, int] = {
    'jan': 1,  'january': 1,
    'feb': 2,  'february': 2,
    'mar': 3,  'march': 3,
    'apr': 4,  'april': 4,
    'may': 5,
    'jun': 6,  'june': 6,
    'jul': 7,  'july': 7,
    'aug': 8,  'august': 8,
    'sep': 9,  'september': 9,
    'oct': 10, 'october': 10,
    'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}


def is_month_name(value: str) -> bool:
    """Return True if the string is a recognized month name."""
    return str(value).strip().lower() in MONTH_NAME_MAP


# ---------------------------------------------------------------------------
# Course code normalization
# ---------------------------------------------------------------------------

# Explicit remappings that can't be handled by generic stripping
_CODE_REMAP: dict[str, str] = {
    'MOC': 'MOC',    # MOC(B1), MOC(B2), MOC(B1 & B2) all → MOC
    'MoC': 'MOC',
    'OR-I': 'OR(I)',
    'ET': 'ETB',     # "ET Workshop" → ETB
}

_SPECIAL_KEYWORDS = frozenset([
    'exam', 'quiz', 'workshop', 'holiday', 'orientation',
    'registration', 'guest session', 'inauguration', 'holiday',
])


def normalize_course_code(cell_text: str) -> str:
    """
    Return the normalized base course code from a raw cell value.

    Examples:
      'OB-1'               → 'OB'
      'FRACM-14'           → 'FRACM'
      'MOC(B1) - S7 (...)'  → 'MOC'
      'OR(I)-3'            → 'OR(I)'
      'OR-I Exams 10.00 AM' → 'OR(I)'
      'OB Quiz - 1'        → 'OB'
      'BS Exams 10.00 AM'  → 'BS'
      'ET Workshop'        → 'ETB'
    """
    text = cell_text.strip()

    # MOC variants (must come before generic hyphen split because of parens)
    if re.match(r'^MOC\b', text, re.IGNORECASE):
        return 'MOC'

    # OR-I variants
    if re.match(r'^OR-I\b', text, re.IGNORECASE):
        return 'OR(I)'

    # ET Workshop → ETB
    if re.match(r'^ET\s+Workshop', text, re.IGNORECASE):
        return 'ETB'

    # OR(I)-N pattern — preserve parenthesized suffix
    m = re.match(r'^(OR\([^)]+\))-', text)
    if m:
        return m.group(1).upper()

    # Generic "CODE(suffix) - ..." → strip from first '('
    m = re.match(r'^([A-Z]+)\s*\(', text, re.IGNORECASE)
    if m:
        code = m.group(1).upper()
        return _CODE_REMAP.get(code, code)

    # "CODE - N" or "CODE Quiz - N" or "CODE Exams ..."
    m = re.match(r'^([A-Z()]+)\s*[-\s]', text, re.IGNORECASE)
    if m:
        prefix = m.group(1).strip('-').upper()
        return _CODE_REMAP.get(prefix, prefix)

    return text.upper()


def is_special_event(cell_text: str) -> bool:
    """Return True for non-regular sessions (exams, quizzes, workshops, holidays, etc.)."""
    lower = cell_text.strip().lower()
    return any(kw in lower for kw in _SPECIAL_KEYWORDS)


def extract_session_number(cell_text: str) -> str:
    """
    Extract the session / occurrence number from a cell string.

    'OB-1'               → '1'
    'FRACM-14'           → '14'
    'MOC(B1) - S7'       → 'S7'
    'MOC(B1 & B2) - S12' → 'S12'
    'OB Quiz - 1'        → 'Quiz-1'
    'BS Exams 10.00 AM'  → 'Exam'
    'ET Workshop'        → 'Workshop'
    'Inauguration ...'   → ''
    'Registration ...'   → ''
    """
    text = cell_text.strip()

    # MOC(Bx) - SN  or  MOC(B1 & B2) - SN
    m = re.search(r'-\s*S(\d+)', text, re.IGNORECASE)
    if m:
        return f'S{m.group(1)}'

    # CODE-N  (regular session number)
    m = re.search(r'-(\d+)\b', text)
    if m:
        return m.group(1)

    # Quiz variants
    m = re.search(r'quiz\s*[-–]?\s*(\d+)', text, re.IGNORECASE)
    if m:
        return f'Quiz-{m.group(1)}'

    if re.search(r'\bexam', text, re.IGNORECASE):
        return 'Exam'

    if re.search(r'\bworkshop', text, re.IGNORECASE):
        return 'Workshop'

    return ''


# ---------------------------------------------------------------------------
# Date reconstruction
# ---------------------------------------------------------------------------

def reconstruct_date(day_num: int, month_name: str, year: int) -> date:
    """
    Build a date from integer day, month name string, and year.

    Raises ValueError for unrecognized month names.
    """
    key = month_name.strip().lower()
    if key not in MONTH_NAME_MAP:
        raise ValueError(
            f"Unrecognized month name: {month_name!r}. "
            f"Expected a full or abbreviated English month name."
        )
    month_num = MONTH_NAME_MAP[key]
    return date(year, month_num, day_num)


# ---------------------------------------------------------------------------
# Google Sheets URL → spreadsheet ID
# ---------------------------------------------------------------------------

def extract_spreadsheet_id(sheet_url: str) -> str:
    """
    Extract the spreadsheet ID from any valid Google Sheets URL.

    Handles:
      https://docs.google.com/spreadsheets/d/<ID>/edit#gid=0
      https://docs.google.com/spreadsheets/d/<ID>/edit?usp=sharing
      https://docs.google.com/spreadsheets/d/<ID>
      Plain ID strings (passed through unchanged if no slash present)
    """
    m = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', sheet_url)
    if m:
        return m.group(1)
    # If no URL pattern, assume the raw string is already an ID
    if re.fullmatch(r'[a-zA-Z0-9_-]+', sheet_url):
        return sheet_url
    raise ValueError(
        f"Cannot extract spreadsheet ID from: {sheet_url!r}\n"
        "Expected format: https://docs.google.com/spreadsheets/d/<ID>/..."
    )


# ---------------------------------------------------------------------------
# Year extraction from sheet metadata rows
# ---------------------------------------------------------------------------

def extract_year_from_metadata(rows: list[list]) -> int:
    """
    Scan the first 5 rows for a 4-digit year (e.g. '2026' in the title).
    Falls back to the current year with a warning if not found.
    """
    import datetime
    for row in rows[:5]:
        for cell in row:
            if cell is None:
                continue
            m = re.search(r'(\b20\d{2}\b)', str(cell))
            if m:
                return int(m.group(1))
    current = datetime.date.today().year
    logger.warning("Could not detect year from sheet metadata; defaulting to %d.", current)
    return current


# ---------------------------------------------------------------------------
# Row padding
# ---------------------------------------------------------------------------

def pad_rows(rows: list[list], width: int = 15) -> list[list]:
    """
    Pad each row to `width` elements with None.

    The Sheets API omits trailing empty cells, so rows may be shorter than
    the expected 15 columns (A–O).
    """
    return [row + [None] * max(0, width - len(row)) for row in rows]
