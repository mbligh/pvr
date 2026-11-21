# PWR recording matcher

`match_releases` associates recordings in `pvr.releases` with canonical
fixtures in `pvr.episodes`. It does not download media, choose a recording
quality, create files, or modify any episode's canonical season/number.

The PWR matcher lives in `matchers/pwr.py`, separate from the general
Atlas-backed CLI so future sports can have their own matching rules.

## Rules

* Recognize both teams, in home-away order, in a recording's title. Canonical
  PWR names: Bristol Bears, Exeter Chiefs, Gloucester-Hartpury, Harlequins,
  Leicester Tigers, Loughborough Lightning, Sale Sharks, Saracens and
  Trailfinders Women.
* Accept known spelling and presentation variants such as Gloucester Hartpury,
  Saracens Women, Trailfinders and Ealing Trailfinders. It does not perform
  arbitrary string-similarity guesses.
* Use the release's `published` field (which YouTube derives preferentially
  from the original livestream timestamp). Convert timestamp values to
  Europe/London date, then require a fixture within **±2 calendar days**.
* A fixture is selected only when there is **exactly one** matching home/away
  fixture in that date window. Missing dates, missing historical fixtures and
  ambiguous candidates never produce automatic links.
* Ignore matches explicitly flagged unavailable and titles describing
  highlights, previews, interviews, youth or Next Gen Cup content.
* Never replace an existing `episode_id`, whether matched previously or
  entered manually. Multiple recordings from different providers can link
  to the same episode. The matching operation only writes release fields
  (`episode_id`, `match_method`, `match_details`, `matched_at`).

The match date is evidence, not an absolute guarantee: different publishers
can release footage a day or two after the fixture. Other competitions and
mislabelled releases might need explicit manual review.

## Running on slagg

Using system Python and the existing `~/.mongodb/pvr` URI file:

```sh
cd ~/pvr
git pull

# Preview proposed associations. No writes.
python3 match_releases

# Show why each YouTube recording did or did not match.
python3 match_releases --source youtube --verbose

# After inspecting the preview, store only unique matches.
python3 match_releases --apply
```

Standard output contains JSON Lines for candidate, linked, ambiguous and
conflicting releases; `--verbose` includes unmatched releases. Standard
error contains a JSON summary of counts. `--source bbc`, `--source youtube`
and `--source rugbypass` restrict the source. `--tolerance-days N` allows
adjusting the date tolerance (0–7 days, default 2).

The operation is idempotent. Re-running it reports already-linked releases
without changing them; the existing daily `import_rugby` cron job is
unaffected.

## Missing historical PWR fixtures

The initial `import_rugby` stored 75 PWR fixtures in **Season_2026**
(2026/27), but no PWR fixtures from **Season_2025** (2025/26). A YouTube
recording of a 2025/26 game will correctly report `no_fixture`; the matcher
must not assign it to a 2026/27 episode based on the same teams. Importing
historical 2025/26 fixtures and stable numbering is a separate task.
