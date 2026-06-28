# TimeTable Project

## What This Does

Fetches a timetable from a Google Drive Excel file (.xlsx) and converts it into a `.ics` calendar file. The `.ics` is published to GitHub Pages via a GitHub Actions workflow that runs three times daily.

**Repository:** `LarflleezeGonzo/TimeTable` on GitHub  
**Published calendar URL:** `https://larflleezeglonzo.github.io/TimeTable/timetable.ics` (served from the `gh-pages` branch)

---

## Project Structure

| File | Purpose |
|------|---------|
| `timetable_sources.json` | **Add new terms here** — lists all Drive files to sync |
| `sync_timetable.py` | CLI entry point — loads sources, fetches all terms, merges into one `.ics` |
| `auth.py` | Google OAuth2 flow; caches token in `token.json` |
| `sheets.py` | Downloads the Excel file from Google Drive and parses it |
| `ics_generator.py` | Builds the `.ics` bytes from parsed events |
| `utils.py` | Helpers (spreadsheet ID extraction, year detection, code normalization) |
| `.env` | Local config: `SHEET_URL` (Term I), `DEFAULT_OUTPUT`, `DEFAULT_TIMEZONE`, `LOCATION` |
| `.github/workflows/sync.yml` | CI: runs sync 3×/day, deploys `timetable.ics` to `gh-pages` |

### Adding a new term

Edit `timetable_sources.json` and append an entry:
```json
{ "term": "Term III", "url": "https://docs.google.com/spreadsheets/d/NEW_FILE_ID/edit" }
```
Commit and push — the next CI run picks it up automatically. No secrets needed (URLs are not sensitive).

---

## Credentials — Where They Live

### Local development

Two files are needed locally and are **gitignored** — never commit them:

- **`credentials.json`** — OAuth2 client secrets downloaded from Google Cloud Console.  
  To create one:  
  1. Go to [Google Cloud Console](https://console.cloud.google.com)  
  2. Enable the **Google Drive API** for your project  
  3. APIs & Services → Credentials → Create Credentials → **OAuth client ID**  
  4. Application type: **Desktop app**  
  5. Download the JSON → rename to `credentials.json` → place in project root

- **`token.json`** — Cached OAuth token written automatically after first browser sign-in. Delete it to force re-authentication.

### GitHub Actions (CI)

Secrets are stored in **GitHub → Settings → Secrets and variables → Actions**:

| Secret | Contents |
|--------|---------|
| `CREDENTIALS_JSON` | Full contents of `credentials.json` |
| `TOKEN_JSON` | Full contents of `token.json` |
| `SHEET_URL` | Term I Google Drive file URL (referenced by `timetable_sources.json`) |

The workflow writes credential files at runtime — they are never logged or committed.

> Term II and later terms have their URLs hardcoded in `timetable_sources.json` — no new secrets needed when adding a term.

---

## Running Locally

```bash
# Install dependencies (uses uv)
uv sync

# Run the sync (reads SHEET_URL from .env)
uv run python sync_timetable.py

# Or pass the URL directly
uv run python sync_timetable.py --sheet-url 'https://docs.google.com/spreadsheets/d/FILE_ID/edit'

# Dry run — print events without writing the .ics
uv run python sync_timetable.py --dry-run --verbose
```

On first run a browser window opens for Google sign-in. Subsequent runs reuse `token.json` silently.

---

## GitHub Actions Workflow

File: `.github/workflows/sync.yml`

- **Schedule:** 7:30 AM, 9:55 AM, and 2:05 PM IST daily
- **Manual trigger:** GitHub Actions UI → "Run workflow"
- **Steps:** checkout → install deps → write credential files from secrets → run sync → deploy `timetable.ics` to `gh-pages` via `peaceiris/actions-gh-pages`

The `gh-pages` branch is managed entirely by the workflow; do not push to it manually.

---

## Pushing Changes

```bash
# Normal commit and push to main
git add <files>
git commit -m "your message"
git push origin main
```

The CI workflow triggers automatically on its schedule (not on push). To test a workflow change immediately, use **GitHub Actions UI → Run workflow**.

---

## Gitignored Files

These files must never be committed:

- `credentials.json` — Google OAuth client secrets
- `token.json` — cached OAuth access/refresh token
- `timetable.ics` / `timetable.ics.tmp` — generated output
- `.env` — local config with the sheet URL
- `*.xlsx` — timetable source files (e.g. `Term-I, Timetable - One Year MBA (DEM) 2026-27.xlsx`)
- `.venv/`, `.DS_Store`, `__pycache__/`
