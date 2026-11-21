"""Teletz Reddit F1 release extraction (no network or database imports).

One verified Reddit comment may contain multiple independently identified
TransferNow releases. Preserve the source observation even when no supported
release can be parsed. Reddit is untrusted input; extraction never downloads.
"""
import datetime as dt
import hashlib
import re
import unicodedata
from urllib.parse import urlparse

SOURCE = "reddit-teletz"
TRANSFER = re.compile(
    r"https?://(?:www\.)?transfernow\.net/dl/([A-Za-z0-9]{8,64})",
    re.IGNORECASE,
)
HEADER = re.compile(r"^(SKY|F1TV|MULTI|DUTCH|GPR|WORLD[\s-]*FEED)\b", re.IGNORECASE)
EXTRA_SHOW = re.compile(r"\b(?:pre|post)[\s-]*race\s*show\b", re.IGNORECASE)
RESOLUTION = re.compile(r"\b(720p|1080p|2160p|4k)\b", re.IGNORECASE)
SESSION_SUFFIXES = (
    ("ss", re.compile(r"^(?:sprint\s+(?:qualifying|shootout)|sprint\s+quali)$", re.I)),
    ("s", re.compile(r"^(?:sprint|sprint\s+race)$", re.I)),
    ("p1", re.compile(r"^(?:fp\s*1|(?:free\s+)?practice\s*1)$", re.I)),
    ("p2", re.compile(r"^(?:fp\s*2|(?:free\s+)?practice\s*2)$", re.I)),
    ("p3", re.compile(r"^(?:fp\s*3|(?:free\s+)?practice\s*3)$", re.I)),
    ("q", re.compile(r"^(?:qualifying|quali)$", re.I)),
    ("r", re.compile(r"^(?:race|grand\s+prix)$", re.I)),
)
F1_TITLE = re.compile(r"\b(20\d{2})\s+(?:F1|Formula\s*1)\s*-\s*(.+)", re.I)
NORMAL_ALIASES = {
    "greatbritain": "british", "uk": "british", "britain": "british",
    "netherlands": "dutch", "holland": "dutch",
    "brazil": "saopaulo", "brasil": "saopaulo",
    "usa": "unitedstates", "us": "unitedstates", "austin": "unitedstates",
    "mexico": "mexicocity",
}


def canonical_name(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"\b(?:grand\s+prix|gp)\b", "", value, flags=re.I)
    compact = re.sub(r"[^a-z0-9]", "", value.lower())
    return NORMAL_ALIASES.get(compact, compact)


def thread_identity(title):
    """Return (year, canonical GP, code), or None rather than fuzzy guessing."""
    match = F1_TITLE.search(title or "")
    if not match:
        return None
    chunks = re.split(r"\s+-\s+", match.group(2).strip())
    if len(chunks) < 2:
        return None
    code = next((key for key, pattern in SESSION_SUFFIXES
                 if pattern.fullmatch(chunks[-1].strip())), None)
    venue = canonical_name(" - ".join(chunks[:-1]))
    if not code or not venue:
        return None
    return int(match.group(1)), venue, code


def match_episode(title, episodes):
    """Match only an exact normalized name, year and session code."""
    key = thread_identity(title)
    if key is None:
        return None
    candidates = [ep["_id"] for ep in episodes
                  if (ep.get("season"), canonical_name(ep.get("meeting_name") or ""),
                      ep.get("session_code")) == key]
    return candidates[0] if len(candidates) == 1 else None


def clean_text(line):
    return re.sub(r"[*_~" + chr(96) + r"]+", "", line).strip()


def restore_links(body, links):
    """Add hrefs omitted by Reddit's rendered-label text extraction."""
    if not isinstance(links, list):
        return body
    position = 0
    for item in links:
        if isinstance(item, str):
            url, label = item, ""
        elif isinstance(item, dict):
            url, label = item.get("url") or "", item.get("label") or ""
            if isinstance(label, list):
                label = "".join(label)
        else:
            continue
        if not TRANSFER.search(url) or url in body:
            continue
        if label:
            idx = body.find(label, position)
            if idx >= 0:
                point = idx + len(label)
                body = body[:point] + " " + url + body[point:]
                position = point + len(url) + 1
                continue
        body += "\n" + url
    return body


def split_sections(body):
    sections = [{"heading": "", "lines": []}]
    for line in body.splitlines():
        cleaned = clean_text(line)
        if HEADER.match(cleaned) and (
                RESOLUTION.search(cleaned) or "commentary" in cleaned.lower()
                or EXTRA_SHOW.search(cleaned)):
            sections.append({"heading": cleaned, "lines": []})
        else:
            sections[-1]["lines"].append(line)
    return sections


def get_commentary(heading):
    hit = HEADER.match(heading)
    kind = hit.group(1).lower().replace(" ", "-") if hit else "unknown"
    return {"gpr": "dutch", "world-feed": "world-feed"}.get(kind, kind)


def release_records(comment):
    """Convert one verified comment to independent PVR release objects."""
    title = comment.get("post_title") or ""
    body = restore_links(comment.get("body") or "", comment.get("links") or [])
    if not F1_TITLE.search(title):
        return []
    comment_id = str(comment["id"]).removeprefix("t1_")
    post_id = str(comment.get("post_id") or "").removeprefix("t3_")
    releases, found = [], set()
    for section in split_sections(body):
        heading = section["heading"]
        if EXTRA_SHOW.search(heading):
            continue
        context = "\n".join(section["lines"])
        commentary = get_commentary(heading)
        quality = RESOLUTION.search(heading)
        height = (2160 if quality and quality.group(1).lower() in ("2160p", "4k")
                  else 1080 if quality and quality.group(1).lower() == "1080p"
                  else 720 if quality else None)
        sky_audio = commentary == "sky" or (
            commentary == "multi" and bool(re.search(r"\bEnglish\s+Sky\b", context, re.I)))
        uploader = ""
        if " - " in heading:
            uploader = heading.rsplit(" - ", 1)[1].split("(")[0].strip()
        for line in section["lines"]:
            variant_match = re.match(r"^\s*(?:[*_]*)(5\.1|stereo|surround|atmos)\s*:", line, re.I)
            audio_variant = variant_match.group(1).lower() if variant_match else None
            for transfer in TRANSFER.finditer(line):
                transfer_id = transfer.group(1)
                if transfer_id in found:
                    continue
                found.add(transfer_id)
                link = "https://www.transfernow.net/dl/" + transfer_id
                delivery = "stream/download" if re.search(r"\[Stream/DL\]", line, re.I) else "download"
                rank = (0 if commentary == "sky" else
                        1 if sky_audio else
                        2 if commentary == "f1tv" else 3)
                metadata = {
                    "url": link, "commentary": commentary, "contains_sky_audio": sky_audio,
                    "height": height, "audio_variant": audio_variant,
                    "uploader": uploader or None, "delivery": delivery,
                    "preference_rank": rank,
                    "heading": heading or None,
                    "thread_title": title, "reddit_comment_id": comment_id,
                    "reddit_thread_id": post_id, "reddit_url": comment.get("url"),
                    "observation_id": "reddit:comment:" + comment_id,
                }
                created = comment.get("created") or ""
                release = {
                    "source": SOURCE, "id": transfer_id,
                    "uri": SOURCE + ":" + transfer_id,
                    "title": title + " - " + (heading or "Unknown source"),
                    "type": "video", "metadata": metadata,
                }
                if re.match(r"^\d{4}-\d{2}-\d{2}", created):
                    release["published"] = created[:10]
                releases.append(release)
    return releases


def observation(comment):
    """Preserve raw content and provenance independently of extracted URLs."""
    comment_id = str(comment["id"]).removeprefix("t1_")
    body = comment.get("body") or ""
    links = comment.get("links") or []
    return {
        "_id": "reddit:comment:" + comment_id,
        "source": SOURCE, "kind": "comment",
        "reddit_comment_id": comment_id,
        "thread_id": str(comment.get("post_id") or "").removeprefix("t3_"),
        "thread_title": comment.get("post_title"),
        "author": comment.get("author"), "parent_author": comment.get("parent_author"),
        "subreddit": comment.get("subreddit"),
        "created": comment.get("created"), "url": comment.get("url"),
        "body": body, "links": links,
        "content_sha256": hashlib.sha256(
            (body + "\n" + repr(links)).encode("utf-8")).hexdigest(),
    }


def eligible(comment):
    """Only teletz's verified top-level AutoModerator F1 comments."""
    return (isinstance(comment, dict) and
            (comment.get("author") or "").lower() == "teletz" and
            (comment.get("subreddit") or "").lower() == "motorsportsreplays" and
            comment.get("depth") == 0 and
            (comment.get("parent_author") or "").lower() == "automoderator" and
            bool(comment.get("id")) and bool(comment.get("post_id")) and
            bool(F1_TITLE.search(comment.get("post_title") or "")))


def process_comments(comments):
    bundles = []
    seen = set()
    for comment in comments:
        if not eligible(comment):
            continue
        obs = observation(comment)
        if obs["_id"] in seen:
            raise ValueError("Duplicate Reddit comment " + obs["_id"])
        seen.add(obs["_id"])
        releases = release_records(comment)
        bundles.append({"observation": obs, "releases": releases})
    return bundles


def fetch_live(pages=3, method="stdlib", tools_dir=None):
    """Use the same redditlib and AutoModerator verification as watch-teletz."""
    import sys
    from pathlib import Path

    path = Path(tools_dir).expanduser() if tools_dir else Path.home() / "tools" / "reddit"
    if not (path / "redditlib.py").is_file():
        raise RuntimeError(f"Missing {path / 'redditlib.py'}; clone/update mbligh/tools")
    sys.path.insert(0, str(path))
    from redditlib import RedditClient, parse_page

    client = RedditClient(method=method)
    result = client.fetch_records("https://www.reddit.com/user/teletz/comments/",
                                  max_pages=pages)
    verified = {}
    comments = []
    for comment in result["comments"]:
        if not (isinstance(comment, dict) and
                (comment.get("author") or "").lower() == "teletz" and
                (comment.get("subreddit") or "").lower() == "motorsportsreplays" and
                comment.get("depth") == 0 and
                F1_TITLE.search(comment.get("post_title") or "") and
                comment.get("post_id")):
            continue
        post_id = str(comment["post_id"]).removeprefix("t3_")
        if post_id not in verified:
            url = f"https://www.reddit.com/r/MotorsportsReplays/comments/{post_id}/"
            parsed = parse_page(client.fetch(url).body)
            posts = [p for p in parsed["posts"]
                     if (p.get("id") or "").lower() == "t3_" + post_id.lower()]
            if not posts or not posts[0].get("author"):
                raise RuntimeError("Cannot verify AutoModerator thread author: " + url)
            verified[post_id] = posts[0]
        parent = verified[post_id]
        if (parent.get("author") or "").lower() != "automoderator":
            continue
        comment = dict(comment)
        comment["parent_author"] = parent["author"]
        if not comment.get("post_title"):
            comment["post_title"] = parent.get("title")
        if not (comment.get("body") or "").strip():
            parsed = parse_page(client.fetch(comment["url"]).body)
            same = [c for c in parsed["comments"] if c.get("id") == comment.get("id")]
            if not same or not (same[0].get("body") or "").strip():
                raise RuntimeError("Cannot recover body of " + str(comment.get("url")))
            comment["body"] = same[0]["body"]
            comment["links"] = same[0].get("links") or []
        comments.append(comment)
    return comments, result["pages_fetched"]
