# Rugby match officials and commentary enrichment

Run from slagg after installing dependencies with the repository requirements.

    cd ~/pvr
    ./import_rugby_crew
    ./import_rugby_crew --apply

The command is **preview-only by default**. With \`--apply\`, it updates only
existing episode documents in Atlas \`pvr.episodes\` and never creates fixtures,
downloads media, touches files, or persists scores or match results.

Sources:
- World Rugby official match pages, located by existing \`world-rugby-UUID\`
  fixture IDs. The labelled Match Officials table yields referee, assistants,
  TMO, and foul-play review officer for international fixtures and WXV.
- PWR's fixtures API, located by existing \`pwr-NNN\` fixture IDs, yields
  referee and touch judges for PWR. Only official fields are extracted.
- ICDb's public sitemap and per-match pages yield broadcast-specific
  commentary crews. Matching requires both teams, competition, and kickoff
  within four hours. Entries for different broadcasters remain separate.
  Sitemap history and commentator coverage are incomplete.

Stored fields:
- \`match_officials\`: role keys \`referee\`, \`assistant_referee_1\`,
  \`assistant_referee_2\`, \`tmo\`, \`fpro\`, plus \`source\` and \`url\`
  (and \`fixture_id\` for PWR).
- \`broadcast_commentary\`: array of \`{channel, people: [{role, name,
  language}], source, url}\` entries. Existing entries from other channels
  are preserved, even if absent from a later site fetch.

Only nonempty results update fields; no data is erased if a page is missing
or blocked. Writes are idempotent when nothing changes. All fetched URLs
are allowlisted; HTTP errors are logged as JSONL and do not stop other
sources. Some ICDb requests may be rate-limited or challenged by Cloudflare.
Do not run aggressively against the providers.

## World Rugby throttling

World Rugby requests are **sequential**, with a minimum 10-second gap
between request starts (change with \`--world-delay\`) and a maximum of 24
pages per invocation (change with \`--max-world\`). Existing World Rugby
official assignments are skipped; use \`--refresh-officials\` to force
rechecking them.

On HTTP 429, 403, or 503, this importer **stops contacting World Rugby**
for the remainder of that run, while continuing PWR/ICDb. It honors
\`Retry-After\` where supplied and otherwise backs off at least one hour.
\`--apply\` saves the retry deadline in Atlas \`metadata_state\` under
\`rugby_crew_world_rugby\` so cron invocations cannot immediately repeat
the denied requests. Preview mode never writes this cooldown.

When a World Rugby page has no officials, \`--apply\` records
\`crew_world_rugby_checked_at\` on the existing episode. The same empty
page is not requested for seven days by default
(\`--empty-recheck-days\`), allowing historical backfills to progress.
No invented official names or placeholder crew entries are stored.

For a slower backfill:

    ./import_rugby_crew --apply --world-delay 15 --max-world 12


Examples:

    ./import_rugby_crew --apply --days 60 --ahead 14
    ./import_rugby_crew --apply --start 2026-09-01 --end 2026-11-30
    ./import_rugby_crew --apply --series Rugby_Womens_PWR --source pwr
    ./import_rugby_crew --apply --series Rugby_Mens_Nations --source world-rugby
    ./import_rugby_crew --limit 10

By default, it checks the previous 30 days and following 7 days. The
\`--limit\` option limits episode selection, and \`--max-icdb\` limits
ICDb match page requests (default 60). JSONL output contains a preview
or write summary but does not include scores.

Optional slagg cron after deploying the script:

    40 3 * * * cd /home/mbligh/pvr && ./import_rugby_crew --apply >> /home/mbligh/.log/pvr-crew 2>&1

Tests:

    python3 -m unittest discover -s tests -p 'test_rugby_crew.py' -v
