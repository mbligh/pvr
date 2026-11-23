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
    ./indexers/youtube --search "Lightning"

BBC uses the iPlayer iBL catalogue and works for BBC Sport online-only
programmes which don't appear in the regular get_iplayer broadcast cache.
The default brand is PWR (`m001mnb5`); configure other brands with
`--brand`.

YouTube delegates channel/playlist enumeration to an installed `yt-dlp`.
By default the indexer combines the PWR Official **/streams** tab and the
[curated full-match playlist](https://www.youtube.com/playlist?list=PL6NsZFkjlRqEZH4Pi5o9RoHfIfwzeo04l),
not the noisy `/videos` tab. It retrieves full **per-video metadata without
downloading media** for streams, then checks the full-match playlist in flat
mode and only fetches individual metadata for video IDs not already seen.
Both sources share the same `youtube:<id>` URI; duplicates are indexed once.
This recovers uploaded full-match replays that do not appear under `/streams`.
Use `--url <YouTube URL>` to scan one source explicitly instead of the default
combined scan; use `--flat` to enumerate both sources without per-video lookups.
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


## Combined source catalogue refresh and matching

The new one-shot `refresh_releases` CLI fetches BBC iPlayer PWR brand
episodes, metadata-enriched YouTube PWR streams, and RugbyPass featured
full-match replays. It validates all source records before writing any
of the current discovery batch to Atlas, reuses the `index_releases`
importer, then runs the existing conservative PWR fixture matcher.

```sh
# Discovery-only preview (queries providers; does not write Atlas):
python3 refresh_releases

# Refresh and match unambiguous releases:
python3 refresh_releases --apply

# Refresh just YouTube with 100 videos:
python3 refresh_releases --source youtube --youtube-limit 100 --apply

# Query RugbyPass using the existing authenticated local script:
python3 refresh_releases --source rugbypass --rugbypass-pages 5 --apply
```

Optional flags: `--source` (repeatable for bbc/youtube/rugbypass),
`--bbc-pages`, `--youtube-limit`, `--rugbypass-pages`,
`--rugbypass` (default `~/tools/video/rugbypass`) and `--yt-dlp`.

Preview fetches current source listings but **does not match new releases
against Atlas until --apply**. Existing links are never overwritten by
matching. Unlike a full historical archive, the RugbyPass listing only
covers currently featured/accessible sections and iPlayer only returns
currently listed episodes. The worker `run_rugby --apply` checks Atlas's per-source poll schedule
before invoking source discovery and bounded downloads. See POLLING.md.
See [ACQUISITION.md](ACQUISITION.md).

## International RugbyPass matching

After `refresh_releases --apply` imports the RugbyPass JSONL catalogue,
it also invokes `match_rugbypass` for numbered men's internationals,
women's internationals, Nations Championship and the separate WXV
series (WXV fixture migration pending).
The original PWR match remains separate. All associations are conservative
and preserve existing links; the importer's RugbyPass source listing remains
limited to accessible/featured full-match sections. For detailed matching
rules and preview commands see [MATCHING.md](MATCHING.md).


## Rugby24 men's Premiership and Champions Cup replay indexer

`indexers/rugby24` scans **both** Premiership (`/premiership-rugby`) and
EPCR Champions Cup (`/champions-cup`) categories, each with its own
`--pages` budget (`?page2` etc). It visits full-match articles and emits
stable `rugby24:<article-slug>` release URIs only when they carry a
validated `https://ok.ru/videoembed/<id>` iframe.

Titles are classified as `league`, `cup` (Premiership Rugby Cup), or
`champions-cup`. Highlights and Challenge Cup entries are excluded.
Only `league` can match `Rugby_Mens_Prem`, and only `champions-cup` can
match `Rugby_Mens_Championship`. Matches require exactly one fixture
with home/away team aliases and a ±2-day date window; ambiguous cases are
not linked. The public site may block requests with HTTP 403.

    ./indexers/rugby24 --pages 2 | ./index_releases --dry-run
    ./indexers/rugby24 --url https://rugby24.net/champions-cup --pages 2
    ./refresh_releases --source rugby24 --rugby24-pages 2 --apply
    ./match_prem              # preview PREM fixture links
    ./match_championship      # preview Champions Cup fixture links

Refresh runs both matchers after indexing. `run_rugby --series
Rugby_Mens_Championship --apply` or `run_rugby --series Rugby_Mens_Prem
--apply` uses Rugby24 discovery and acquisition with the same
polling, retry, locking and provenance rules. Both categories share a
temporary cookie jar and a 0.5-second delay across requests.

### Rugby24 request client and HTTP 403 diagnostics

Rugby24 uses the system `curl` executable with a browser User-Agent,
as confirmed to return HTTP 200 on slagg. It reuses a temporary cookie
jar across category and article requests and delays 0.5 seconds between
requests. No `curl_cffi` Python dependency or privileged pip install is
required. The previous `urllib` request with the PVR User-Agent returned
HTTP 403; the exact reason for that difference is not established.
Rugby24 may send an HTTP Location header in response to an HTTPS request.
The adapter follows the redirect manually, upgrading that same-host URL
back to HTTPS before issuing another request. It does not follow HTTP
transport, leave rugby24.net, or retry redirects indefinitely. An HTTPS
redirect loop is an actionable site-side configuration error.
The indexer still rejects non-Rugby24 URLs/redirects, failed HTTP requests,
oversized pages and missing playable embeds. HTTP errors report safe
server/challenge headers and the affected request path.
