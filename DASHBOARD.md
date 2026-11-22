# Local PVR dashboard (`pvr_dashboard`)

Read-only, dark-mode web interface to Atlas `pvr.episodes`, with installed
recording state verified against slagg's `/data/sports` filesystem.
No Docker, npm, external CDNs, reverse proxy or additional Python packages
beyond the existing `requirements.txt`. The server uses Python's
standard library and existing PyMongo dependency.

## Start on slagg

```sh
cd ~/pvr
git pull --ff-only

# Run in the foreground, listen on slagg itself:
./pvr_dashboard
# On slagg: http://127.0.0.1:8787/

# Access from another computer on a trusted LAN:
./pvr_dashboard --bind 0.0.0.0 --port 8787
# From another computer: http://slagg:8787/
```

The default listener is **127.0.0.1 only**, deliberately not accessible
from the LAN. Binding 0.0.0.0 exposes the page on all interfaces,
**without authentication**. Use this only on a trusted LAN and block
WAN/DMZ access to TCP 8787 (or bind specifically to slagg's LAN IP).
The service does not modify any media or Atlas records.

For detached operation, first ensure no other process is using the port:

```sh
cd ~/pvr
nohup ./pvr_dashboard --bind 0.0.0.0 --port 8787   > /home/mbligh/pvr-dashboard.log 2>&1 < /dev/null &
```

Optional persistent startup using the existing user crontab
(`crontab -e`) without Docker or systemd:

```cron
@reboot cd /home/mbligh/pvr && exec ./pvr_dashboard --bind 0.0.0.0 --port 8787 >> /home/mbligh/pvr-dashboard.log 2>&1
```

No service or cron entry is installed automatically by the repository.
Stop with Ctrl+C in foreground or send SIGTERM to the `pvr_dashboard`
process started via nohup/cron. The server closes its Atlas connection
on shutdown.

## Page behavior

- The top selector includes exactly the 13 canonical recording series:
  `Formula_1`; `Rugby_Mens_6N`, `Rugby_Mens_Intl`,
  `Rugby_Mens_Lions`, `Rugby_Mens_Nations`, `Rugby_Mens_Prem`,
  `Rugby_Mens_World_Cup`; `Rugby_Womens_6N`, `Rugby_Womens_Intl`,
  `Rugby_Womens_Lions`, `Rugby_Womens_PWR`,
  `Rugby_Womens_World_Cup`, `Rugby_Womens_WXV`.
- Each selected series lists **every** Atlas episode with an event time
  at most **seven days into the future**. Historical events are
  included without a lower cutoff, sorted newest first.
- Fixture rows include start time in configured local timezone,
  season/episode (or `Unnumbered`), title, status, resolution / FPS /
  codec, actual file size and recording source when known.
- Date-only fixtures are shown without an invented clock time.
  Undated fixtures remain visible, sorted to the bottom. Explicitly
  cancelled/superseded episodes remain visible and annotated.
- `Installed` means a nonempty media file **physically exists inside
  that series's `/data/sports/<series>` directory**. For numbered episodes
  the authoritative Atlas `nfo_path` is checked (MKV plus alternate common
  video extensions). Other paths require a positive Atlas `files`
  episode-ID association and an existing regular file within the
  selected series directory. No fuzzy title matching.
- Atlas `files.media.video` quality is used when the inventory record
  describes the current file. Otherwise the page automatically asks
  `/api/quality` to run `ffprobe` only for installed rows as they scroll
  into view; probe metadata is cached in RAM keyed by actual file
  identity, size and modification time. Probe calls are capped at
  two concurrent processes and 12 seconds each. Failures display
  `Quality unavailable`; the file still counts as installed.
- The page is read-only: it does **not** fetch providers, trigger
  downloads, overwrite any files or write to Atlas.
- `/favicon.ico` serves the England Rugby ball icon (16 and 32 px), linked
  from the HTML head for browser tabs and bookmarks. After `git pull` on
  slagg, restart a running `pvr_dashboard` process to load the change.

## Options

```sh
./pvr_dashboard --help
./pvr_dashboard --database pvr --sports-root /data/sports   --timezone America/New_York --ffprobe ffprobe   --bind 127.0.0.1 --port 8787
```

Atlas credentials are read from `MONGODB_URI` or `~/.mongodb/pvr`,
matching the existing PVR CLI. The `/healthz` endpoint returns a basic
HTTP health response; it does not reveal credentials or perform writes.

## Tests

```sh
cd ~/pvr
python3 -m unittest discover -s tests -p 'test_pvr_dashboard.py' -v
```

Tests use fake Atlas collections and temporary media trees, including
real localhost HTTP requests. The server does not need a live Atlas
connection to run these tests.
