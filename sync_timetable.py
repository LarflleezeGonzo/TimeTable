"""
sync_timetable.py — CLI entry point for the timetable → .ics converter.

Reads timetable_sources.json to discover all term sources, fetches each from
Google Drive, merges the events, and writes a single timetable.ics.

Usage:
    uv run python sync_timetable.py [options]

On first run a browser window opens for Google sign-in.
Subsequent runs reuse the cached token silently.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from auth import get_credentials
from ics_generator import build_ics, write_ics
from sheets import (
    TimetableEvent,
    fetch_excel_from_drive,
    parse_calendar_grid,
    parse_course_map,
)
from utils import extract_spreadsheet_id, extract_year_from_metadata


# ---------------------------------------------------------------------------
# Sources loader
# ---------------------------------------------------------------------------

def load_sources(sources_file: str) -> list[dict]:
    """
    Load term sources from a JSON file.

    Each entry must have a "term" name and either:
      "url"     — literal Google Drive/Sheets URL or file ID
      "url_env" — name of an environment variable that holds the URL

    Entries whose URL cannot be resolved (missing env var) are skipped with
    a warning so a partial sync still succeeds.
    """
    path = Path(sources_file)
    if not path.exists():
        return []

    try:
        entries = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Cannot parse {sources_file}: {exc}") from exc

    resolved: list[dict] = []
    for entry in entries:
        term = entry.get("term", "Unknown")
        url  = entry.get("url")
        env  = entry.get("url_env")

        if url:
            resolved.append({"term": term, "url": url})
        elif env:
            val = os.environ.get(env, "").strip()
            if val:
                resolved.append({"term": term, "url": val})
            else:
                print(f"WARNING: {term} skipped — env var '{env}' is not set.")
        else:
            print(f"WARNING: {term} skipped — entry has neither 'url' nor 'url_env'.")

    return resolved


# ---------------------------------------------------------------------------
# Per-term fetch + parse
# ---------------------------------------------------------------------------

def fetch_term_events(
    term: str,
    url: str,
    credentials,
    year_override: int | None,
    location: str,
) -> list[TimetableEvent]:
    """Fetch one Drive file and return its parsed events."""
    try:
        spreadsheet_id = extract_spreadsheet_id(url)
    except ValueError as exc:
        print(f"ERROR [{term}]: bad URL — {exc}")
        return []

    print(f"[{term}] Downloading from Drive (file ID: {spreadsheet_id})…")
    try:
        rows = fetch_excel_from_drive(spreadsheet_id, credentials)
    except PermissionError as exc:
        print(f"ERROR [{term}]: {exc}")
        return []
    except FileNotFoundError as exc:
        print(f"ERROR [{term}]: {exc}")
        return []
    except Exception as exc:
        print(f"ERROR [{term}]: download failed — {exc}")
        return []

    print(f"[{term}] Fetched {len(rows)} rows.")

    course_map = parse_course_map(rows)
    print(f"[{term}] Courses: {len(course_map)} — {', '.join(sorted(course_map))}")

    year = year_override or extract_year_from_metadata(rows)
    print(f"[{term}] Year: {year}")

    events = parse_calendar_grid(rows, course_map, year=year, location=location)
    special = sum(1 for e in events if e.is_special)
    print(f"[{term}] Events: {len(events)} ({len(events) - special} regular, {special} special)\n")

    return events


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_timetable",
        description=(
            "Fetch timetable(s) from Google Drive and convert to a .ics calendar file.\n"
            "Sources are read from timetable_sources.json by default."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  uv run python sync_timetable.py\n"
            "  uv run python sync_timetable.py --sources custom_sources.json\n"
            "  uv run python sync_timetable.py --sheet-url 'https://docs.google.com/...'\n"
            "  uv run python sync_timetable.py --dry-run --verbose\n"
            "\n"
            "Source resolution order:\n"
            "  1. --sources FILE (default: timetable_sources.json)\n"
            "  2. --sheet-url URL / SHEET_URL env var (single-source fallback)\n"
        ),
    )

    parser.add_argument(
        "--sources",
        metavar="FILE",
        default="timetable_sources.json",
        help="JSON file listing timetable sources (default: timetable_sources.json).",
    )
    parser.add_argument(
        "--sheet-url",
        metavar="URL",
        default=None,
        help="Single Google Drive URL — overrides timetable_sources.json.",
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
        help="IANA timezone (default: Asia/Kolkata or DEFAULT_TIMEZONE env var).",
    )
    parser.add_argument(
        "--credentials",
        metavar="FILE",
        default="credentials.json",
        help="Path to OAuth2 credentials JSON (default: credentials.json).",
    )
    parser.add_argument(
        "--token",
        metavar="FILE",
        default="token.json",
        help="Path to cached token file (default: token.json).",
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
        help="Override the timetable year (auto-detected per source by default).",
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
    load_dotenv(override=False)

    parser = _build_parser()
    args   = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(levelname)s  %(name)s: %(message)s")

    output   = args.output   or os.environ.get("DEFAULT_OUTPUT",   "timetable.ics")
    timezone = args.timezone or os.environ.get("DEFAULT_TIMEZONE", "Asia/Kolkata")
    location = args.location or os.environ.get("LOCATION",         "CR-7C-15")

    # --- Resolve sources ---
    if args.sheet_url:
        # Single-URL override — wraps the URL as a one-item source list
        sources = [{"term": "Timetable", "url": args.sheet_url}]
    else:
        fallback_url = os.environ.get("SHEET_URL", "").strip()
        sources = load_sources(args.sources)
        if not sources and fallback_url:
            print(f"INFO: {args.sources} not found; falling back to SHEET_URL env var.")
            sources = [{"term": "Timetable", "url": fallback_url}]

    if not sources:
        parser.error(
            f"No timetable sources found.\n"
            f"Either create {args.sources}, pass --sheet-url, or set SHEET_URL in .env."
        )

    print(f"Sources        : {len(sources)} term(s)")
    print(f"Output file    : {output}")
    print(f"Timezone       : {timezone}")
    print(f"Location       : {location}")
    if args.dry_run:
        print("[DRY RUN — no file will be written]")
    print()

    # --- Authenticate once for all sources ---
    print("Authenticating with Google…")
    try:
        creds = get_credentials(args.credentials, args.token)
    except FileNotFoundError as exc:
        sys.exit(f"ERROR: {exc}")
    except Exception as exc:
        sys.exit(f"ERROR during authentication: {exc}")
    print("Authentication OK.\n")

    # --- Fetch and parse each source ---
    all_events: list[TimetableEvent] = []
    for source in sources:
        events = fetch_term_events(
            term=source["term"],
            url=source["url"],
            credentials=creds,
            year_override=args.year,
            location=location,
        )
        all_events.extend(events)

    if not all_events:
        sys.exit("ERROR: No events parsed from any source.")

    all_events.sort(key=lambda e: (e.date, e.slot_index))
    total   = len(all_events)
    special = sum(1 for e in all_events if e.is_special)
    print(f"Total events   : {total} ({total - special} regular, {special} special)\n")

    if args.dry_run:
        _print_dry_run_table(all_events)
        return

    # --- Build and write .ics ---
    print("Generating .ics…")
    try:
        ics_bytes = build_ics(all_events, timezone_str=timezone)
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


def _print_dry_run_table(events: list[TimetableEvent]) -> None:
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
