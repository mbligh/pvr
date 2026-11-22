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
