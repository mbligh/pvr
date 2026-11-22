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
(2026/27), but no earlier seasons. The separate
[historical PWR importer](PWR_HISTORY.md) can now populate **Season_2025**
(2025/26) from the verified PWR API feed, using the same numbering and
score-free NFO generator. Until it has been run with `--apply`, YouTube
recordings of 2025/26 games correctly report `no_fixture`. Older matches
from 2024/25 and other seasons remain unmatched until their fixtures have
been imported.

## RugbyPass men's/women's internationals and Nations Championship

The separate `match_rugbypass` command matches already indexed RugbyPass
**full-match replays** against the numbered canonical fixtures in Atlas for
`Rugby_Mens_Intl`, `Rugby_Womens_Intl`, `Rugby_Mens_Nations`, and
`Rugby_Womens_WXV` (created on the next live WXV-aware metadata import).
PWR matching is unchanged.

```sh
cd ~/pvr
git pull

# Explain RugbyPass-to-fixture associations without modifying Atlas:
python3 match_rugbypass

# Include unmatched, ambiguous and ineligible catalogue records:
python3 match_rugbypass --verbose

# After inspecting, store only unique links:
python3 match_rugbypass --apply

# Restrict to one series:
python3 match_rugbypass --series Rugby_Mens_Nations
```

Rules for automatic matches:

- **Competition labels determine the canonical destination series.** The
  RugbyPass label `Nations Championship` maps **only** to
  `Rugby_Mens_Nations`, even though the same matches are present in the
  men's-international calendar (usually with `NC:` prefixes).
  `Women's International` maps to `Rugby_Womens_Intl`, while `WXV`
  maps to `Rugby_Womens_WXV`, matching the separate
  `Rugby_Womens_WXV` directory. Existing WXV fixtures in Atlas still
  belong to `Rugby_Womens_Intl`; the matcher now leaves those replays
  unmatched rather than publishing them under the wrong directory.
  Their dedicated numbered fixture/NFO records must be imported separately.
  Explicit men's-international, Nations Cup, Pacific Nations Cup and
  Rugby's Greatest Rivalry labels map to `Rugby_Mens_Intl`.
  Unlabelled content is ignored; a missing gender label is not assumed
  to mean men. `Nations Cup` is not `Nations Championship`.
- Require a title whose last component is `Full Match Replay` and whose
  first component contains exactly one `home v away` pairing.
  Highlights, commentary, previews and old vault matches with no
  relevant competition label are excluded.
- Match **home and away in the correct order**, using normalized accents,
  national flag emoji, `NC:` prefixes, gender markers and a small explicit
  nickname list (All Blacks/New Zealand, Wallabies/Australia,
  Springboks/South Africa, USA/United States). `Australia A` remains
  distinct from `Australia`; `England A` remains distinct from
  `England`. No fuzzy similarity or score-based association.
- RugbyPass's `published` field is **replay publication**, not kickoff.
  Require publication between four hours before the fixture kickoff and
  three days afterwards (configurable `--max-delay-days 0..7`).
  Dates must include timezone information.
- If multiple fixtures have identical sides within that window, return
  `ambiguous`, never pick the closest. Duplicate World Rugby/Google
  fixture entries for some women's matches are a real example.
  Unnumbered fixtures (or those lacking canonical NFO XML/path) return
  `unnumbered_fixture` and are not linked until the importer provides
  complete metadata.
- Unavailable or known sub-one-hour recordings are excluded. Already linked
  releases are never changed. Every successful link includes competition,
  teams, published and scheduled timestamps, publication delay and matching
  method in Atlas `match_details`.

`refresh_releases --apply` now runs this matcher after importing the
RugbyPass catalogue, in addition to the existing PWR matcher. The source
catalogue is not an exhaustive historical replay listing; matches absent
from its accessible sections remain undiscovered. An unnumbered Nations
Championship fixture may become linkable after a later `import_rugby`
updates its canonical episode number/NFO.

See [ACQUISITION.md](ACQUISITION.md) for preview-only acquisition and safe
canonical filing of those matches.
