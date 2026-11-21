# Release discovery indexers

The indexers discover **available recordings**, not canonical episodes. All
commands emit one JSON object per line to stdout. Use stderr for diagnostics.
This does not download media or assign season/episode numbers.

The shared release contract is:

    {"source":"bbc","id":"m0034hxp","uri":"bbc:m0034hxp",
     "title":"2026/27: Loughborough Lightning v Trailfinders",
     "published":"2026-11-20","type":"episode","metadata":{"brand":"m001mnb5"}}

Required: source, id, uri, title. Optional: type, published, duration,
available, metadata. Stable `uri` is the MongoDB release document `_id`.
Publication time is not assumed to be the fixture kickoff time. Discovery
records may not include reliable video technical quality; that is measured
after download.

## Running the indexers

On slagg (system Python, no virtual environment):

    cd ~/pvr
    python3 indexers/bbc --brand m001mnb5 --search "Trailfinders"
    python3 indexers/youtube --url https://www.youtube.com/@ThePWROfficial/streams --search "Lightning"

BBC uses the iPlayer iBL catalogue and works for BBC Sport online-only
programmes which don't appear in the regular get_iplayer broadcast cache.
The default brand is PWR (`m001mnb5`); configure other brands with
`--brand`.

YouTube delegates channel/playlist enumeration to an installed `yt-dlp`.
The default `--url` is the PWR Official **/streams** tab, not `/videos`.
By default it retrieves full **per-video metadata without downloading media**;
this is slower than a flat playlist but can recover dates for archived streams.
The original livestream `release_timestamp` (or `release_date`) takes
priority over the upload `timestamp` (or `upload_date`). The selected date
appears in `published`, and `metadata.date_basis` states which field provided
it; original date fields are also retained in `metadata`. Dates are evidence
for matching fixtures, not definitive match identifiers.

To quickly enumerate titles/IDs without per-video requests, use:

    python3 indexers/youtube --flat --search "Lightning"

Entries explicitly marked live, upcoming or still being processed are
excluded. During full metadata extraction, scheduled upcoming YouTube
livestreams can make `yt-dlp` exit nonzero even with `--ignore-errors`.
The indexer skips **only** known `This live event will begin in ...` errors
while retaining successfully extracted recordings. Other errors remain fatal
so a broken source does not silently result in an incomplete import.
A flat-playlist entry with missing status is included but is
*not asserted to be playable*. After refreshing the PVR checkout, you can
backfill release dates without changing other release state:

    set -o pipefail
    python3 indexers/youtube | python3 index_releases

This resolves metadata for every item in the configured list (up to 100 by
default), so it can be substantially slower and place more requests on
YouTube. If individual videos are unavailable, some dates may remain absent;
do not guess a fixture assignment from team names alone.

RugbyPass is its own indexer, not a second wrapper. Update your existing tools
checkout to the revision adding `--jsonl`, then use:

    python3 ~/tools/video/rugbypass --list-videos --jsonl
    python3 ~/tools/video/rugbypass --list-games --jsonl --search "France"

Existing RugbyPass auth, catalogue pagination, access checks and normal
human-readable output are preserved. It enumerates the accessible *featured
homepage catalogue*, **not the complete RugbyPass archive**. `--pages N`
can look deeper; `--show-restricted` is supported with `--list-games`.
RugbyPass's standard `--export-catalog` remains unchanged.

## Upserting into Atlas

Use a shell pipeline; check failure of the indexer with `pipefail`:

    set -o pipefail
    python3 indexers/bbc | python3 index_releases
    python3 indexers/youtube | python3 index_releases
    python3 ~/tools/video/rugbypass --list-videos --jsonl | python3 index_releases

`index_releases` validates the entire JSONL batch before any DB writes,
reads `~/.mongodb/pvr` (or `MONGODB_URI`), and upserts into the `pvr.releases`
collection. `--dry-run` only validates input. Each release is uniquely
identified by its URI and indexed by (source, id). Each upsert updates
`last_seen` while preserving `first_seen`; records not seen again are
not deleted. The importer never overwrites local file state, selected episode
associations, or download jobs.

The next stage is matching release titles/dates to episodes in MongoDB,
quality selection and downloader dispatch. These indexers and the importer
currently make no such decisions. They are run manually for validation, not
by the existing daily rugby metadata cron job.


## Teletz Formula 1 Reddit indexer

The independent indexers/reddit_teletz adapter uses the verified public
Reddit client in ~/tools/reddit/redditlib.py to revisit top-level comments
by u/teletz on AutoModerator r/MotorsportsReplays threads. Each TransferNow
link becomes its own release in the shared pvr.releases collection, never a
separate collection for Reddit. The default JSONL stream works with the
existing index_releases validator.

For complete provenance and automatic conservative matching to the
F1 episodes, use --bundle and the preview-first import_teletz consumer.
It maintains pvr.source_observations containing full comments and
content hashes to detect edits. It does not change watch-teletz email cron.
See REDDIT_TELETZ.md for commands and limitations.
