# Formula 1 metadata importer

The official Formula 1 event listing and meeting timetables are fetched by
[f1_data](f1_data). The executable [import_f1](import_f1) turns its JSON
snapshot into PVR episodes. It is independent of rugby ingestion and does
not modify media files, releases, or existing calendars.

## Episode identity and numbering

- One series: formula-1 (display title "Formula 1"), seasons are calendar years.
- Exclude pre-season testing entirely.
- Episode number = championship round times 10 + fixed session offset.
- Offsets: FP1=1, FP2=2, FP3=3, Sprint Qualifying=4, Sprint=5,
  Qualifying=6, Grand Prix Race=7.
- Use official session codes p1, p2, p3, ss, s, q, r rather than
  the API's occasionally misleading sessionType field.
- Only actual sessions become episodes; sparse numbers are intentional.
  Normal Round 12: 121, 122, 123, 126, 127.
  Sprint Round 12: 121, 124, 125, 126, 127.
- Stable ID is f1:<meetingSessionKey>. External IDs preserve meetingKey
  and meetingSessionKey. Rescheduling does not change the primary ID.

The upstream US/Central legacy timezone label is normalized to America/Chicago,
which avoids missing-link errors on minimal Debian timezone installations.
The IANA timezone database (system tzdata package) is still required.

Local start and end times, timezone and GMT offset are checked and converted
to UTC in Atlas. The local date is used for NFO aired. Validation rejects
duplicate episode numbers, duplicate session IDs, unknown session codes,
mismatched detail meeting keys, invalid dates and incompatible offsets
before any database writes.

Each PVR episode includes session title, UTC start/end, duration, meeting,
round, circuit/venue, official IDs and spoiler-free episode NFO XML with a
suggested path under Formula_1/Season_YYYY/. Results from the raw source
are not copied into the episode records.

## Usage

Fetch and preview 2026 data without MongoDB:

    ./f1_data --year 2026 > f1-2026.json
    ./import_f1 --input f1-2026.json

Export full normalized records for inspection:

    ./import_f1 --input f1-2026.json --jsonl > f1-episodes.jsonl

Fetch the source data directly and preview:

    ./import_f1 --year 2026

Only when ready, upsert to Atlas:

    ./import_f1 --input f1-2026.json --apply

The --apply flag uses MONGODB_URI or ~/.mongodb/pvr (plain MongoDB URI)
and MONGODB_DATABASE (default: pvr), matching import_rugby. Conflicts with
existing Formula 1 episode numbering are refused. Existing records not
present in the source are not deleted. Operational fields such as release,
file and job associations are not intentionally touched.

Preview and JSONL need only Python 3 standard library; applying requires
python3-pymongo. No F1 import is scheduled automatically.
