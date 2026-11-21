# Teletz Reddit Formula 1 release indexer

This indexer extracts TransferNow offers from u/teletz's *top-level* comments
on AutoModerator threads in r/MotorsportsReplays. It reuses the Reddit HTML
client and challenge handling already maintained in mbligh/tools/reddit,
without changing watch-teletz's email state, cron entry, or behavior.

It does not fetch media, follow TransferNow URLs, test availability or bypass
download restrictions. Every run rereads the Reddit listing, including edits
to comments still on the fetched profile pages.

## Design

- indexers/reddit_teletz: fetches/validates comments and emits release JSONL.
  No database writes. --bundle instead emits one JSONL object per verified
  comment: {observation:{...},releases:[...]}.
- import_teletz: validates the entire bundle, stores raw comment content in
  pvr.source_observations and individual releases in the existing shared
  pvr.releases collection. The shared index_releases code validates and
  upserts release records.
- Source observations have _id=reddit:comment:COMMENT_ID and preserve full
  body, title, post/comment IDs, author, URL, outbound links and a content
  fingerprint. first_seen, last_seen, content_changed_at are maintained.
- Releases have _id=reddit-teletz:TRANSFERNOW_ID, the stable short TransferNow
  URL in metadata.url, and source commentary, video height, audio variant,
  uploader and Reddit provenance. The same link appearing in several comments
  remains one release with all observed source IDs.
- Only unambiguous F1 thread title matches are associated with pvr.episodes
  by matching season, canonical Grand Prix name and session (including sprint).
  Unknown/ambiguous titles remain unlinked. Existing episode_id associations
  are preserved and conflicts are rejected before any writes.
- Preference ranks are advisory for the future selector: 0=dedicated Sky,
  1=MULTi with English Sky, 2=F1TV, 3=other or unknown. A dedicated Sky
  1080p release therefore ranks above MULTi 4K for now.
- Pre-race and post-race *show* links are ignored as releases, but the full
  raw comment is kept. Commentary and quality are inferred from each block's
  heading and notes. No exact expiry date or download availability is assumed.

## Usage on slagg

Requires the system Python and an up-to-date ~/tools/reddit/redditlib.py.
The regular email watcher does not need changes.

Run a read-only preview of extracted release records:

    cd ~/pvr
    ./indexers/reddit_teletz --pages 3

See both raw observations and parsed releases, without contacting Atlas:

    ./indexers/reddit_teletz --pages 3 --bundle | ./import_teletz

Check matching against the 2026 F1 episodes already imported to Atlas.
This reads but does not write to MongoDB:

    set -o pipefail
    ./indexers/reddit_teletz --pages 3 --bundle | ./import_teletz --match

After inspecting the output, update the database:

    set -o pipefail
    ./indexers/reddit_teletz --pages 3 --bundle | ./import_teletz --apply

Use MONGODB_URI or ~/.mongodb/pvr, and MONGODB_DATABASE (default pvr).
No records or files are deleted because a Reddit link disappears or expires.
Nothing is downloaded and no emails are sent by this indexer.

For offline fixture regression tests, the indexer supports --input JSONFILE
with a comments array. Each comment must include parent_author=AutoModerator
from an already verified source. A raw tools/reddit/reddit --json snapshot
does not independently verify the parent author and cannot be imported
as trusted data until that verification has been performed.

To use the existing common importer *without* raw observations or auto-matching:

    set -o pipefail
    ./indexers/reddit_teletz --pages 3 | ./index_releases

## Notes and limitations

- Revisit the recent profile pages rather than using watch-teletz's emailed
  ID history. A post edited after it falls outside the requested --pages
  cannot be detected without a broader historical backfill.
- The Reddit client must obtain verified HTML; an error fails the run rather
  than treating an empty/blocked listing as a genuine empty catalogue.
- There is no PVR downloader for TransferNow yet. Recording acquisition and
  source-quality validation remain separate tasks.
- Initial parser tests cover the supplied single-Sky and multi-source
  examples, including F1TV 5.1/stereo, Sky, Dutch, MULTi 1080p/4K and
  exclusion of pre/post-race shows. This is not yet a complete live
  historical backtest of every comment format.
- These are short-lived links; keep the original timestamps and first/last
  seen times, and independently check link availability before acquisition.
- Cron is not changed automatically. Start with manual --match/--apply
  checks; once verified, schedule a separate PVR indexer job (including
  Thursday/Monday) while retaining watch-teletz email notifications.
