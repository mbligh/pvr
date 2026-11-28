# Publishing PVR indexer health

The executable `publish_status` reads `pvr.indexer_poll_state` in Atlas
using the existing PVR MongoDB credentials. It publishes one public-safe
snapshot of the seven current indexers, excluding the obsolete shared
Rugby24 state. It does not discover, download, write to Atlas or change
any polling schedules. Missing indexer records show Unknown.

## Setup

1. Deploy the updated mbligh/dashboard Worker source
   (`worker/src/index.mjs`) in Cloudflare's Edit Code UI. Keep the existing
   STATUS_EVENTS KV binding and GitHub webhook secret.
2. On slagg generate a separate shared secret:

   ```sh
   cd ~/pvr
   git pull --ff-only
   mkdir -p ~/.config/pvr
   (umask 077; test ! -e ~/.auth/pvr-dashboard-secret &&
       openssl rand -hex 32 > ~/.auth/pvr-dashboard-secret)
   chmod 600 ~/.auth/pvr-dashboard-secret
   ```

   Copy the file's value privately into a new Cloudflare Worker **Secret**
   named EXTERNAL_STATUS_SECRET_PVR. Do not commit or paste it into chat.
   Deploy the secret setting.
3. Preview (read-only), then publish:

   ```sh
   ./publish_status
   ./publish_status --apply
   ```

   Inspect https://dashboard.mbligh.org/ after KV propagation. A missing
   snapshot is explicitly marked unavailable; no secret reaches the browser.
4. Add to the slagg user's crontab (ensure ~/.log exists):

   ```cron
   */5 * * * * /home/mbligh/pvr/publish_status --apply >> /home/mbligh/.log/pvr-status 2>&1
   ```

The publisher runs independently of run_rugby/run_f1, even on days when
they are not scheduled. Each five-minute invocation compares an exact
digest of the seven safe indexer records against the last successful
publication in ~/.cache/pvr/dashboard-status.json. It publishes on
changes or at least hourly, at most 288 writes/day in normal cron use,
usually much fewer. The heartbeat detects publisher/network outages;
the dashboard marks snapshots stale after 90 minutes. A heartbeat does
not imply each indexer has run recently; its own last-attempt timestamp
is displayed separately. A lock prevents overlapping local publishers.
A failed publish does not advance the local state. Use only one publisher
for this group, with a synchronized clock; KV is eventually consistent.

The only public information sent is indexer names, status, consecutive
failure count, last attempt, last success, retry deadline and a fixed
sanitized failure reason. No raw exceptions, indexer media titles, credentials,
internal paths or hostnames are sent. Requests use HTTPS and a
body-specific HMAC-SHA256 signature with a signed publication timestamp;
redirects are rejected. The dashboard Worker has a separate group secret
and stores one persistent KV key status:pvr. It performs no additional
KV list operations. Local preview does not require the dashboard secret.

The dashboard implementation is in mbligh/dashboard. Worker redeployment
and cron installation are manual; pushing these repositories does not
perform those steps.

## Downloader status

The same signed snapshot now contains five downloader rows (BBC, YouTube,
RugbyPass, Rugby24 and TransferNow) from existing `pvr.jobs` records. The
latest success and failure are selected independently by finish time; the
latest started job, if running, is checked against its existing host-local
provider lock without creating a file. A released lock means interrupted;
remote hosts or inaccessible locks show activity unknown. No job records
are modified. Existing history is included without a migration. No progress
percentage is reported. The browser formats start and finish timestamps in
its local timezone, including daylight-saving changes. Five-minute publishing
can miss short active downloads but retains their completed results.

**Public information change:** canonical episode titles and job timestamps
are published to the public dashboard; raw errors, source URIs, internal
paths, hosts and credentials remain excluded. Failures use fixed categories.
Deploy the updated dashboard Worker first, then pull the PVR repository on
slagg; the existing cron and secret need no change. An old Worker silently
strips the new section, so after redeploying it, remove the local publisher
cache or wait for the hourly heartbeat if an update has already been sent.
