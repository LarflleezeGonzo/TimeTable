"""
sync_timetable.py — CLI entry point for the timetable → .ics converter.


Usage:
    uv run python sync_timetable.py --sheet-url <URL> [options]

On first run a browser window opens for Google sign-in.
Subsequent runs reuse the cached token silently.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from auth import get_credentials
from ics_generator import build_ics, write_ics
from sheets import (
    fetch_excel_from_drive,
    parse_calendar_grid,
    parse_course_map,
)
from utils import extract_spreadsheet_id, extract_year_from_metadata


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_timetable",
        description=(
            "Fetch a timetable from Google Sheets and convert it to a "
            "shareable .ics calendar file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  uv run python sync_timetable.py --sheet-url 'https://docs.google.com/spreadsheets/d/SHEET_ID/edit'\n"
            "  uv run python sync_timetable.py --sheet-url 'https://...' --dry-run --verbose\n"
            "  uv run python sync_timetable.py --sheet-url 'https://...' --output my_timetable.ics\n"
            "\n"
            "You can also set SHEET_URL in a .env file to avoid passing --sheet-url every time."
        ),
    )

    parser.add_argument(
        "--sheet-url",
        metavar="URL",
        default=None,
        help="Google Sheets URL (or plain spreadsheet ID). "
             "Can also be set via the SHEET_URL environment variable.",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        default=None,
        help="Output .ics file path (default: timetable.ics or DEFAULT_OUTPUT env var).",
    )
    parser.add_argument(
        "--timezone",
        metavar="TZ",
        default=None,
        help="IANA timezone for event datetimes "
             "(default: Asia/Kolkata or DEFAULT_TIMEZONE env var).",
    )
    parser.add_argument(
        "--credentials",
        metavar="FILE",
        default="credentials.json",
        help="Path to the OAuth2 credentials JSON file (default: credentials.json).",
    )
    parser.add_argument(
        "--token",
        metavar="FILE",
        default="token.json",
        help="Path to the cached token file (default: token.json).",
    )
    parser.add_argument(
        "--location",
        metavar="VENUE",
        default=None,
        help="Venue string added to every event (default: CR-7C-15 or LOCATION env var).",
    )

    parser.add_argument(
        "--year",
        metavar="YYYY",
        type=int,
        default=None,
        help="Override the timetable year (default: auto-detected from sheet title).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print parsed events to stdout without writing the .ics file.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv(override=False)  # .env fills gaps; CLI args take priority

    parser = _build_parser()
    args   = parser.parse_args()

    # --- Logging ---
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s  %(name)s: %(message)s",
    )
    logger = logging.getLogger("sync_timetable")

    # --- Resolve configuration (CLI > .env > defaults) ---
    sheet_url  = args.sheet_url or os.environ.get("SHEET_URL", "").strip()
    output     = args.output    or os.environ.get("DEFAULT_OUTPUT", "timetable.ics")
    timezone   = args.timezone  or os.environ.get("DEFAULT_TIMEZONE", "Asia/Kolkata")
    location   = args.location  or os.environ.get("LOCATION", "CR-7C-15")

    if not sheet_url:
        parser.error(
            "No Google Sheet URL provided.\n"
            "Pass --sheet-url <URL>  or  set SHEET_URL in your .env file."
        )

    # --- Extract spreadsheet ID ---
    try:
        spreadsheet_id = extract_spreadsheet_id(sheet_url)
    except ValueError as exc:
        parser.error(str(exc))

    print(f"Spreadsheet ID : {spreadsheet_id}")
    print(f"Output file    : {output}")
    print(f"Timezone       : {timezone}")
    print(f"Location       : {location}")
    if args.dry_run:
        print("[DRY RUN — no file will be written]")
    print()

    # --- Authenticate ---
    print("Authenticating with Google…")
    try:
        creds = get_credentials(args.credentials, args.token)
    except FileNotFoundError as exc:
        sys.exit(f"ERROR: {exc}")
    except Exception as exc:
        sys.exit(f"ERROR during authentication: {exc}")

    print("Authentication OK.\n")

    # --- Fetch data (Drive API — file is stored as Excel .xlsx on Drive) ---
    print(f"Downloading Excel file from Google Drive (file ID: {spreadsheet_id})…")
    try:
        rows = fetch_excel_from_drive(spreadsheet_id, creds)
    except PermissionError as exc:
        sys.exit(f"ERROR: {exc}")
    except FileNotFoundError as exc:
        sys.exit(f"ERROR: {exc}")
    except Exception as exc:
        sys.exit(f"ERROR downloading file: {exc}")

    print(f"Fetched {len(rows)} rows.\n")

    # --- Parse course map ---
    course_map = parse_course_map(rows)
    print(f"Courses found  : {len(course_map)}")
    for code, info in sorted(course_map.items()):
        print(f"  {code:<8}  {info['name']}")
    print()

    # --- Detect year ---
    year = args.year or extract_year_from_metadata(rows)
    print(f"Timetable year : {year}\n")

    # --- Parse events ---
    events = parse_calendar_grid(rows, course_map, year=year, location=location)
    total  = len(events)
    special = sum(1 for e in events if e.is_special)
    print(f"Events parsed  : {total} ({total - special} regular, {special} special)\n")

    # --- Dry-run output ---
    if args.dry_run:
        _print_dry_run_table(events)
        return

    # --- Build and write .ics ---
    print("Generating .ics…")
    try:
        ics_bytes = build_ics(events, timezone_str=timezone)
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")

    try:
        write_ics(ics_bytes, output)
    except OSError as exc:
        sys.exit(f"ERROR: {exc}")

    size_kb = len(ics_bytes) / 1024
    print(f"Done!  Written {total} events to '{output}' ({size_kb:.1f} KB).")
    print()
    print("Next steps:")
    print(f"  • Import '{output}' into Google Calendar / Apple Calendar / Outlook")
    print("  • Re-run this script anytime to refresh — the file is overwritten cleanly.")


def _print_dry_run_table(events) -> None:
    print(f"{'DATE':<12} {'SLT':>3} {'START':>5} {'END':>5}  {'CODE':<8}  CELL TEXT")
    print("-" * 72)
    for e in events:
        marker = " *" if e.is_special else ""
        print(
            f"{e.date.isoformat():<12} "
            f"{e.slot_index:>3} "
            f"{e.start_time:%H:%M} "
            f"{e.end_time:%H:%M}  "
            f"{e.course_code:<8}  "
            f"{e.cell_text}{marker}"
        )
    print()
    print("* = special event (exam / quiz / workshop / holiday / etc.)")


if __name__ == "__main__":
    main()
