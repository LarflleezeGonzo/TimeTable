"""
term4_parser.py — grid parser for the Term IV (GSCM & DEM combined) timetable.

Term IV differs from Terms I–III in ways the shared parser in sheets.py does
not handle, so it gets its own parser. sheets.py is left untouched so earlier
terms parse exactly as before. Opt in per source with "parser": "term4" in
timetable_sources.json.

Differences handled here:
  * Month labels may carry a year ("Dec'26", "Jan'27"), and the term spans
    New Year. The title row says "2025-26" (a typo), so the year comes from
    the labels, with rollover when the month goes backwards (Dec → Jan).
  * Core courses are split by section: 'CS-S1 (DEM)' / 'CS-S1 (GSCM)'.
    Cells tagged with an excluded section are dropped.
  * Course codes can contain digits ('B2BM-S1', 'B2BM S10').
  * Placeholder cells like '--' are skipped.
  * Footnoted combined cells: 'OME-S6 & OME-S7 *' with a col-J note
    '* OME -S7 (6.30 - 8.00pm)' become two events, OME-S7 at the noted time.
  * Sessions sharing the same date and time are merged into one entry.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from sheets import (
    TimetableEvent,
    _DATA_START_ROW_IDX,
    _COL_DATE,
    _COL_DAY,
    _COL_LABEL,
    _SLOT_COLS,
    _fuzzy_lookup,
    _weekday_matches,
)
from utils import (
    MONTH_NAME_MAP,
    extract_session_number,
    extract_year_from_metadata,
    get_session_times,
    is_special_event,
    normalize_course_code,
    parse_embedded_time,
)

logger = logging.getLogger(__name__)

_COL_NOTE = 9  # J: footnotes like '* OME -S7 (6.30 - 8.00pm)'

# "Jan", "Sept", "Dec'26", "Jan '27", "Feb 2027": optional 2- or 4-digit year
_MONTH_LABEL_RE = re.compile(r"^([A-Za-z]+)\.?\s*(?:['’]\s*(\d{2})|(\d{4}))?$")

# A code that has digits after its first letter, e.g. 'B2BM-S1', 'B2BM S10'
_ALNUM_CODE_RE = re.compile(r"^([A-Za-z]+\d[A-Za-z0-9]*)\s*[-\s]")


def parse_month_label(value: str) -> tuple[int, int | None] | None:
    """
    Parse a column-A month label into (month_number, year_or_None).

    'Jan' → (1, None), "Dec'26" → (12, 2026), 'Feb 2027' → (2, 2027).
    Returns None for week labels like 'W-1' and other text.
    """
    m = _MONTH_LABEL_RE.match(str(value).strip())
    if not m or m.group(1).lower() not in MONTH_NAME_MAP:
        return None
    month = MONTH_NAME_MAP[m.group(1).lower()]
    if m.group(2):
        return month, 2000 + int(m.group(2))
    if m.group(3):
        return month, int(m.group(3))
    return month, None


def _normalize_code(cell_text: str) -> str:
    """normalize_course_code, plus support for codes containing digits (B2BM)."""
    m = _ALNUM_CODE_RE.match(cell_text.strip())
    if m:
        return m.group(1).upper()
    return normalize_course_code(cell_text)


def parse_term4_grid(
    rows: list[list],
    course_map: dict[str, dict],
    year: int | None = None,
    location: str = "CR-7C-15",
    exclude_sections: list[str] | None = None,
) -> list[TimetableEvent]:
    """Parse the Term IV calendar grid into TimetableEvent objects (merged per slot)."""
    if year is None:
        year = extract_year_from_metadata(rows)

    section_re = None
    if exclude_sections:
        alts = "|".join(re.escape(s) for s in exclude_sections)
        section_re = re.compile(rf"\(\s*(?:{alts})\s*\)", re.IGNORECASE)

    events: list[TimetableEvent] = []
    current_month_num: int | None = None
    current_year = year

    for row_idx in range(_DATA_START_ROW_IDX, len(rows)):
        row = rows[row_idx]
        col_a = row[_COL_LABEL]

        parsed = parse_month_label(str(col_a)) if col_a is not None else None
        if parsed:
            month_num, label_year = parsed
            if label_year is not None:
                current_year = label_year
            elif current_month_num is not None and month_num < current_month_num:
                current_year += 1
            current_month_num = month_num

        day_cell = row[_COL_DATE]
        if day_cell is None or current_month_num is None:
            continue
        try:
            day_num = int(float(str(day_cell)))
            event_date = date(current_year, current_month_num, day_num)
        except (ValueError, TypeError) as exc:
            logger.warning("Row %d: skipping — %s", row_idx + 1, exc)
            continue

        expected_day = row[_COL_DAY]
        if expected_day and not _weekday_matches(event_date, str(expected_day)):
            logger.warning(
                "Row %d: day mismatch — sheet says '%s' but %s is a %s. Skipping row.",
                row_idx + 1, str(expected_day).strip(), event_date.isoformat(),
                event_date.strftime("%a"),
            )
            continue

        note = str(row[_COL_NOTE] or "").strip()
        note_key = re.sub(r"\s+", "", note).upper() if note.startswith("*") else ""
        note_time = parse_embedded_time(note) if note_key else None

        for slot_index, col_idx in _SLOT_COLS.items():
            cell = row[col_idx]
            if cell is None:
                continue
            raw = str(cell).strip()
            if not re.search(r"[A-Za-z0-9]", raw):   # blank or '--'
                continue
            if section_re and section_re.search(raw):
                continue

            # 'A & B *' + footnote naming B with a time → separate events
            parts = [raw]
            if note_time and raw.endswith("*") and "&" in raw:
                parts = [p.strip(" *") for p in raw.split("&")]

            for cell_str in parts:
                if len(parts) > 1 and re.sub(r"\s+", "", cell_str).upper() in note_key:
                    start_t, end_t = note_time
                else:
                    embedded = parse_embedded_time(cell_str)
                    start_t, end_t = embedded if embedded else get_session_times(slot_index)
                _append_event(events, row_idx, slot_index, cell_str, start_t, end_t,
                              event_date, course_map, location)

    return merge_concurrent_events(events)


def _append_event(events, row_idx, slot_index, cell_str, start_t, end_t,
                  event_date, course_map, location) -> None:
    """Build one TimetableEvent from a cell (or part of one) and append it."""
    code = _normalize_code(cell_str)
    info = course_map.get(code.upper()) or _fuzzy_lookup(code, course_map)
    if info is None:
        logger.warning(
            "Row %d slot %d: unrecognized course code '%s' in '%s'.",
            row_idx + 1, slot_index, code, cell_str,
        )

    events.append(TimetableEvent(
        date=event_date,
        slot_index=slot_index,
        cell_text=cell_str,
        course_code=code,
        course_name=info["name"] if info else cell_str,
        faculty=info["faculty"] if info else "",
        session_number=extract_session_number(cell_str),
        start_time=start_t,
        end_time=end_t,
        is_special=is_special_event(cell_str),
        location=location,
    ))


def merge_concurrent_events(events: list[TimetableEvent]) -> list[TimetableEvent]:
    """
    Collapse events sharing the same date, start and end time into one entry,
    titled e.g. 'Fintech-S1 (Financial Technology) / B2BM-S1 (B2B Marketing)'.
    """
    groups: dict[tuple, list[TimetableEvent]] = {}
    for e in events:
        groups.setdefault((e.date, e.start_time, e.end_time), []).append(e)

    merged: list[TimetableEvent] = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        title = " / ".join(
            e.cell_text if e.course_name == e.cell_text else f"{e.cell_text} ({e.course_name})"
            for e in group
        )
        first = group[0]
        merged.append(TimetableEvent(
            date=first.date,
            slot_index=first.slot_index,
            cell_text=title,
            course_code=" / ".join(e.course_code for e in group),
            course_name=title,
            faculty="; ".join(e.faculty for e in group if e.faculty),
            session_number="",
            start_time=first.start_time,
            end_time=first.end_time,
            is_special=all(e.is_special for e in group),
            location=first.location,
        ))
        logger.info("Merged %d concurrent events on %s: %s", len(group), first.date, title)
    return merged
