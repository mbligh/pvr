"""Rugby24 OK.ru embed downloader, safely staged through the yt-dlp adapter."""
import re
from urllib.parse import urlsplit

from downloaders.youtube import download_url

IDENTIFIER = re.compile(r"rugby24:([a-z0-9]+(?:-[a-z0-9]+)*)\Z")
VIDEO = re.compile(r"/videoembed/(\d{5,20})\Z")


def rugby24_id(uri):
    match = IDENTIFIER.fullmatch(uri) if isinstance(uri, str) else None
    if match is None or len(match.group(1)) > 128:
        raise ValueError("Expected rugby24:<article-slug> URI")
    return match.group(1)


def embed_from_release(uri, release):
    slug = rugby24_id(uri)
    metadata = release.get("metadata") or {}
    page = urlsplit(metadata.get("page_url") or "")
    video = urlsplit(metadata.get("embed_url") or "")
    if (page.scheme != "https" or page.hostname != "rugby24.net"
            or page.path != "/" + slug or page.query or page.fragment
            or video.scheme != "https" or video.hostname != "ok.ru"
            or not VIDEO.fullmatch(video.path) or video.query or video.fragment):
        raise ValueError("Invalid Rugby24 page URL or OK.ru video embed")
    return metadata["embed_url"]


def download(uri, embed, destination, *, yt_dlp="yt-dlp", ffprobe="ffprobe",
             referer=None, **kwargs):
    rugby24_id(uri)
    video = urlsplit(embed)
    if (video.scheme != "https" or video.hostname != "ok.ru"
            or not VIDEO.fullmatch(video.path) or video.query or video.fragment):
        raise ValueError("Invalid OK.ru embed URL")
    return download_url(embed, destination, yt_dlp=yt_dlp, ffprobe=ffprobe,
                        referer=referer, **kwargs)
