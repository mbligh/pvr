"""Local-only PVR HTML dashboard with lazy read-only ffprobe quality details."""
import datetime as dt
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

from inventory.scan import probe_video
from .data import DashboardData, SERIES, filesize, quality


def esc(text):
    return html.escape(str(text), quote=True)


def render_page(selected, rows, *, timezone, updated_at):
    if selected not in SERIES:
        raise ValueError("Unknown series")
    installed = sum(item["installed"] for item in rows)
    upcoming = sum(item["status"] == "Upcoming" for item in rows)
    tabs = []
    for series in SERIES:
        selected_class = " active" if series == selected else ""
        current = ' aria-current="page"' if series == selected else ""
        tabs.append(
            f'<a class="tab{selected_class}" href="/?{urlencode({"series": series})}"'
            f'{current}>{esc(series)}</a>'
        )
    body = []
    for item in rows:
        status = item["status"]
        badge_class = {
            "Installed": "installed", "Missing": "missing",
            "Upcoming": "upcoming", "Cancelled": "cancelled",
            "Superseded": "cancelled",
        }.get(status, "missing")
        if not item["installed"]:
            qcell = '<td class="quality muted">—</td>'
        elif item["quality"]:
            qcell = f'<td class="quality">{esc(item["quality"])}</td>'
        else:
            endpoint = "/api/quality?" + urlencode(
                {"series": selected, "event": item["id"]}
            )
            qcell = (f'<td class="quality waiting" data-quality-url="{esc(endpoint)}">'
                     'Inspecting…</td>')
        title_attr = (item["title"] +
                      (f" · {item['venue']}" if item["venue"] else ""))
        body.append(
            '<tr>'
            f'<td class="date" title="{esc(item["date"])}">{esc(item["date"])}</td>'
            f'<td class="episode mono">{esc(item["number"])}</td>'
            f'<td class="event" title="{esc(title_attr)}"><span>{esc(item["title"])}</span></td>'
            f'<td><span class="status {badge_class}">{esc(status)}</span></td>'
            f'{qcell}'
            f'<td class="size mono">{esc(item["size"])}</td>'
            f'<td class="source">{esc(item["source"] or "—")}</td>'
            '</tr>'
        )
    if not body:
        body.append('<tr><td colspan="7" class="empty">No events found for this series.</td></tr>')
    nav = "".join(tabs)
    table = "".join(body)
    stamp = updated_at.astimezone(timezone).strftime("%d %b %Y, %I:%M %p %Z")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>PVR · {esc(selected)}</title>
<link rel="stylesheet" href="/style.css">
<script src="/dashboard.js" defer></script>
</head>
<body>
<main class="shell">
  <header class="mast">
    <div>
      <div class="brand"><span class="brand-mark">PVR</span><span class="brand-sub">Library dashboard</span></div>
      <h1>{esc(selected)}</h1>
      <p class="intro">Scheduled events and verified recordings · Local library</p>
    </div>
    <div class="actions">
      <span class="updated">Updated {esc(stamp)}</span>
      <a class="refresh" href="/?{urlencode({"series": selected})}">Refresh</a>
    </div>
  </header>
  <nav class="tabs" aria-label="Choose series">{nav}</nav>
  <section class="overview" aria-label="Series summary">
    <div class="metric"><strong>{len(rows):,}</strong><span>Known events</span></div>
    <div class="metric"><strong>{installed:,}</strong><span>Installed</span></div>
    <div class="metric"><strong>{len(rows) - installed - upcoming:,}</strong><span>Not installed</span></div>
    <div class="metric"><strong>{upcoming:,}</strong><span>Upcoming (next 7 days)</span></div>
  </section>
  <section class="listing" aria-label="Event table">
    <div class="table-head"><h2>Events</h2><span>Newest first · Local time ({esc(str(timezone))})</span></div>
    <div class="table-scroll">
      <table>
        <thead><tr>
          <th scope="col">Date / time</th>
          <th scope="col">Episode</th>
          <th scope="col">Event</th>
          <th scope="col">Recording</th>
          <th scope="col">Quality</th>
          <th scope="col">Size</th>
          <th scope="col">Source</th>
        </tr></thead>
        <tbody>{table}</tbody>
      </table>
    </div>
  </section>
  <footer>Only files verified under the selected series directory count as installed.
    Quality is taken from matching Atlas inventory, or inspected when a row enters view.
    No recordings are modified.
  </footer>
</main>
</body>
</html>"""


class QualityLookup:
    """Cache ffprobe metadata by inode/size/mtime without writing to Atlas."""
    def __init__(self, data, *, ffprobe="ffprobe", probe=probe_video):
        self.data = data
        self.ffprobe = ffprobe
        self.probe = probe
        self.cache = {}
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(2)

    def lookup(self, series, episode_id):
        installed = self.data.details(series, episode_id)
        if installed is None:
            return {"installed": False, "quality": "Not installed", "size": "—"}
        description = installed["quality"]
        if not description:
            stat = installed["stat"]
            cache_key = (str(installed["path"]), stat.st_ino, stat.st_size,
                         stat.st_mtime_ns)
            with self.lock:
                description = self.cache.get(cache_key)
            if description is None:
                with self.slots:
                    try:
                        media = self.probe(installed["path"], binary=self.ffprobe, timeout=12)
                        description = quality(media) or "Unknown quality"
                    except (ValueError, OSError):
                        description = "Quality unavailable"
                with self.lock:
                    if len(self.cache) >= 2048:
                        self.cache.clear()
                    self.cache[cache_key] = description
        return {"installed": True, "quality": description,
                "size": filesize(installed["stat"].st_size)}


def make_handler(data, *, quality_lookup=None, now_fn=None):
    lookup = quality_lookup or QualityLookup(data)
    clock = now_fn or (lambda: dt.datetime.now(dt.timezone.utc))
    directory = Path(__file__).parent

    class Handler(BaseHTTPRequestHandler):
        def send(self, content, mime, *, status=200):
            payload = content.encode("utf-8") if isinstance(content, str) else content
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'self'; "
                             "script-src 'self'; connect-src 'self'; "
                             "base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def send_json(self, item, *, status=200):
            self.send(json.dumps(item, ensure_ascii=False),
                      "application/json; charset=utf-8", status=status)

        def do_GET(self):
            try:
                url = urlsplit(self.path)
                path = url.path
                if path in ("/style.css", "/dashboard.js"):
                    name = path.removeprefix("/")
                    mime = ("text/css; charset=utf-8" if name == "style.css"
                            else "application/javascript; charset=utf-8")
                    self.send((directory / name).read_bytes(), mime)
                    return
                if path == "/healthz":
                    self.send_json({"ok": True})
                    return
                params = parse_qs(url.query, keep_blank_values=True)
                if path == "/":
                    selected = params.get("series", [SERIES[0]])[0]
                    if selected not in SERIES:
                        self.send("Unknown series", "text/plain; charset=utf-8",
                                  status=400)
                        return
                    now = clock()
                    rows = data.rows(selected, now=now)
                    html_page = render_page(selected, rows,
                                            timezone=data.timezone, updated_at=now)
                    self.send(html_page, "text/html; charset=utf-8")
                    return
                if path == "/api/quality":
                    selected = params.get("series", [""])[0]
                    eid = params.get("event", [""])[0]
                    if selected not in SERIES or not eid or len(eid) > 512:
                        self.send_json({"error": "Invalid series or event"}, status=400)
                        return
                    self.send_json(lookup.lookup(selected, eid))
                    return
                self.send("Not found", "text/plain; charset=utf-8", status=404)
            except Exception as exc:
                # Never leak Atlas URIs, local paths or provider credentials
                # to clients on an accidentally exposed LAN listener.
                print(f"PVR dashboard request failed: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                self.send("Dashboard unavailable", "text/plain; charset=utf-8",
                          status=503)

        def log_message(self, format, *args):
            print("%s - %s" % (self.address_string(), format % args),
                  file=sys.stderr)

    return Handler
