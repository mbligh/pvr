# pvr

## Local dashboard

Run `./pvr_dashboard` on slagg for a read-only dark-mode series/event
dashboard at `http://127.0.0.1:8787/`. Add `--bind 0.0.0.0` to make
it accessible from a trusted LAN (no login is provided). The dashboard
lists all canonical series, future events through the next seven days,
and installed recordings' verified files and media quality.
See [DASHBOARD.md](DASHBOARD.md) for setup, options and security.
