"""
ics_generator.py — Build an RFC 5545-compliant .ics calendar from TimetableEvent objects.

Design decisions:
  - One VEVENT per class occurrence (no RRULE) — the grid provides exact dates.
  - UIDs are deterministic (SHA-256 of date|slot|cell_text) for idempotent re-runs.
  - The output file is overwritten entirely on each run — no diffing needed.
  - Timezones are handled via the built-in `zoneinfo` module (Python >= 3.9).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icalendar import Calendar, Event, vText, vDatetime

from sheets import TimetableEvent

logger = logging.getLogger(__name__)

_PRODID = "-//IIM Udaipur Timetable Sync//EN"
_DEFAULT_CAL_NAME = "IIM Udaipur Term-I Timetable"


# ---------------------------------------------------------------------------
# UID
# ---------------------------------------------------------------------------

def _build_uid(event: TimetableEvent) -> str:
    """
    Stable, deterministic UID.

    The (date, slot_index, cell_text) triple uniquely identifies any cell in
    the timetable grid.  SHA-256 ensures no collisions across regenerations.
    """
    uid_input = (
        f"{event.date.isoformat()}"
        f"|{event.slot_index}"
        f"|{event.cell_text.strip().upper()}"
    )
    return hashlib.sha256(uid_input.encode()).hexdigest()[:32] + "@iimutimetable"


# ---------------------------------------------------------------------------
# SUMMARY / DESCRIPTION
# ---------------------------------------------------------------------------

def _build_summary(event: TimetableEvent) -> str:
    """
    Human-readable summary shown as the event title in calendar apps.

    Regular:  'OB - Organizational Behaviour (OB-1)'
    Special:  'MOC - Managerial Oral Communication (MOC(B1) - S7)'
    Unmapped: raw cell text
    """
    code = event.course_code
    name = event.course_name

    # If unmapped (name == cell_text) just return the cell text as-is
    if name == event.cell_text:
        return name

    return f"{code} - {name} ({event.cell_text})"


def _build_description(event: TimetableEvent) -> str:
    """Multi-line description shown in the event detail view."""
    lines = []
    if event.faculty:
        lines.append(f"Faculty: {event.faculty}")
    if event.session_number:
        lines.append(f"Session: {event.session_number}")
    if event.course_code and event.course_code != event.course_name:
        lines.append(f"Code: {event.course_code}")
    lines.append("")
    lines.append(f"Raw: {event.cell_text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Calendar builder
# ---------------------------------------------------------------------------

def build_ics(
    events: list[TimetableEvent],
    timezone_str: str = "Asia/Kolkata",
    calendar_name: str = _DEFAULT_CAL_NAME,
) -> bytes:
    """
    Build and return raw .ics bytes from a list of TimetableEvent objects.

    Args:
        events:        Parsed events from sheets.parse_calendar_grid().
        timezone_str:  IANA timezone name (e.g. 'Asia/Kolkata').
        calendar_name: Display name for the calendar (X-WR-CALNAME).

    Raises:
        ValueError: If timezone_str is not a valid IANA timezone.
    """
    try:
        tz = ZoneInfo(timezone_str)
    except ZoneInfoNotFoundError:
        raise ValueError(
            f"Unknown timezone: {timezone_str!r}. "
            "Use an IANA name like 'Asia/Kolkata' or 'Asia/Kolkata'."
        )

    cal = Calendar()
    cal.add("prodid",         vText(_PRODID))
    cal.add("version",        "2.0")
    cal.add("calscale",       "GREGORIAN")
    cal.add("method",         "PUBLISH")
    cal.add("x-wr-calname",   calendar_name)
    cal.add("x-wr-timezone",  timezone_str)
    cal.add("x-wr-caldesc",   "Auto-generated from Google Sheets timetable.")

    for event in events:
        vevent = _build_vevent(event, tz, timezone_str)
        cal.add_component(vevent)

    ics_bytes = cal.to_ical()
    logger.info(
        "Built calendar with %d events (%d bytes).",
        len(events), len(ics_bytes),
    )
    return ics_bytes


def _build_vevent(
    event: TimetableEvent,
    tz: ZoneInfo,
    timezone_str: str,
) -> Event:
    """Construct a single VEVENT component from a TimetableEvent."""
    dtstart = datetime.combine(event.date, event.start_time).replace(tzinfo=tz)
    dtend   = datetime.combine(event.date, event.end_time).replace(tzinfo=tz)

    vevent = Event()
    vevent.add("uid",         _build_uid(event))
    vevent.add("summary",     _build_summary(event))
    vevent.add("description", _build_description(event))
    vevent.add("location",    event.location)
    vevent.add("dtstart",     dtstart)
    vevent.add("dtend",       dtend)
    vevent.add("status",      "TENTATIVE" if event.is_special else "CONFIRMED")

    # CATEGORIES for color-coding in supporting clients
    if event.is_special:
        vevent.add("categories", ["Special"])
    else:
        vevent.add("categories", [event.course_code])

    return vevent


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def write_ics(content: bytes, output_path: str) -> None:
    """
    Write .ics bytes to output_path, overwriting any existing file.

    The file is written atomically (temp file + rename) to avoid partial
    writes leaving a corrupt .ics file.

    Raises:
        OSError: If the parent directory doesn't exist or is not writable.
    """
    dest = Path(output_path)
    tmp  = dest.with_suffix(".ics.tmp")

    try:
        tmp.write_bytes(content)
        tmp.replace(dest)
    except OSError as exc:
        # Clean up temp file if the rename failed
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise OSError(
            f"Could not write calendar to '{output_path}': {exc}"
        ) from exc

    logger.info("Calendar written to '%s'.", output_path)
