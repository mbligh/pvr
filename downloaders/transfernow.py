#!/usr/bin/env python3
"""Download files from public TransferNow links without a browser or API key.

Uses TransferNow's website JSON endpoints (not its separate, keyed developer API).
Requires Python 3.9+ and curl for the actual, resumable file transfer.
"""

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

SITE = "https://www.transfernow.net"
API = SITE + "/api/transfer/downloads"
HEADERS = {
    "Accept": "application/json",
    "Referer": SITE + "/",
    "User-Agent": "Mozilla/5.0 (compatible; TransferNowCLI/1.0)",
}
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{5,128}$")
BLOCKED_TYPES = ("text/html", "application/xhtml+xml", "application/json")


class TransferNowError(Exception):
    """An invalid link, API error, or unsafe download response."""


@dataclass(frozen=True)
class Share:
    transfer_id: str
    user_secret: str = ""
    file_id: str = ""
    share_token: str = ""


def valid_id(value, label):
    if not IDENTIFIER.fullmatch(value):
        raise TransferNowError(f"Invalid {label}: {value!r}")
    return value


def parse_share(input_url):
    """Understand both /dl/ID[/SECRET] and /en/cld?utm_source=ID links."""
    if "://" not in input_url and "/" not in input_url:
        return Share(transfer_id=valid_id(input_url, "transfer ID"))
    parsed = urlparse(input_url)
    if parsed.scheme != "https" or parsed.hostname not in (
        "transfernow.net", "www.transfernow.net"
    ):
        raise TransferNowError("Expected an HTTPS URL on www.transfernow.net")
    query = parse_qs(parsed.query)
    parts = [p for p in parsed.path.split("/") if p]
    if parts and parts[0] in ("en", "fr", "es", "de", "it", "pt", "nl"):
        parts.pop(0)
    if len(parts) >= 2 and parts[0] == "dl":
        transfer_id = parts[1]
        secret = parts[2] if len(parts) > 2 else ""
    elif (parts == ["cld"] or parts[:2] == ["d", "start"]):
        transfer_id = (query.get("utm_source") or [""])[0]
        secret = (query.get("utm_medium") or [""])[0]
    else:
        raise TransferNowError("Not a recognized TransferNow sharing URL")
    file_id = (query.get("utm_term") or [""])[0]
    share_token = (query.get("shareToken") or [""])[0]
    valid_id(transfer_id, "transfer ID")
    if secret:
        valid_id(secret, "download secret")
    if file_id:
        valid_id(file_id, "file ID")
    return Share(transfer_id, secret, file_id, share_token)


def api_json(endpoint, params):
    url = API + "/" + endpoint + "?" + urlencode(params)
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if "application/json" not in content_type:
                raise TransferNowError(
                    f"TransferNow returned {content_type or 'unknown content type'} "
                    "instead of JSON"
                )
            body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise TransferNowError("Unexpectedly large API response")
            data = json.loads(body)
            if not isinstance(data, dict):
                raise TransferNowError("Unexpected TransferNow API format")
            return data
    except HTTPError as exc:
        details = exc.read(1024).decode("utf-8", "replace")
        raise TransferNowError(f"TransferNow API returned HTTP {exc.code}: {details}") from exc
    except (URLError, OSError, ValueError) as exc:
        raise TransferNowError(f"TransferNow API error: {exc}") from exc


def metadata(share, password=""):
    params = {"transferId": share.transfer_id}
    if share.user_secret:
        params["userSecret"] = share.user_secret
    if password:
        params["password"] = password
    data = api_json("metadata", params)
    if data.get("needPassword") and not password:
        raise TransferNowError("Transfer requires a password (set TRANSFERNOW_PASSWORD)")
    if data.get("status") not in (None, "ENABLED"):
        raise TransferNowError(f"Transfer status: {data['status']}")
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise TransferNowError("No downloadable files in this transfer")
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise TransferNowError("Invalid file list returned by TransferNow")
        valid_id(entry.get("id", ""), "file ID")
        if not isinstance(entry.get("size"), int) or entry["size"] < 0:
            raise TransferNowError("Invalid file size returned by TransferNow")
    return files


def select_files(files, file_id=""):
    if not file_id:
        return files
    selected = [f for f in files if f["id"] == file_id]
    if not selected:
        raise TransferNowError(f"File ID {file_id!r} not found in transfer")
    return selected


def signed_url(share, file_id, password=""):
    params = {"transferId": share.transfer_id, "preview": "false", "fileId": file_id}
    if share.user_secret:
        params["userSecret"] = share.user_secret
    if share.share_token:
        params["shareToken"] = share.share_token
    if password:
        params["password"] = password
    data = api_json("link", params)
    url = data.get("url")
    ensure_storage_url(url)
    return url


def ensure_storage_url(url):
    parsed = urlparse(url) if isinstance(url, str) else None
    if not parsed or parsed.scheme != "https" or not parsed.hostname or not (
        parsed.hostname == "transfernow.net" or parsed.hostname.endswith(".transfernow.net")
    ) or parsed.username or parsed.password:
        raise TransferNowError("TransferNow did not return a valid HTTPS storage URL")


def probe(url):
    """Check content type before invoking curl; avoid saving an HTML error as a file.

    HEAD is preferred; if unsupported, read at most one byte from a ranged GET.
    """
    headers = dict(HEADERS)
    headers["Accept"] = "*/*"
    for method in ("HEAD", "GET"):
        req_headers = dict(headers)
        if method == "GET":
            req_headers["Range"] = "bytes=0-0"
        req = Request(url, headers=req_headers, method=method)
        try:
            with urlopen(req, timeout=30) as response:
                ensure_storage_url(response.geturl())
                ctype = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if ctype in BLOCKED_TYPES:
                    raise TransferNowError(
                        f"Refusing to save {ctype} from the storage URL (not a file)"
                    )
                if method == "GET":
                    response.read(1)
                return response.headers.get("Content-Length"), ctype
        except HTTPError as exc:
            if method == "HEAD" and exc.code in (403, 405, 501):
                continue
            raise TransferNowError(f"Storage server returned HTTP {exc.code}") from exc
        except (URLError, OSError) as exc:
            raise TransferNowError(f"Storage probe failed: {exc}") from exc
    raise TransferNowError("Storage server rejected HEAD and ranged GET")


def safe_name(name):
    # Files are named by the sender: never permit directory traversal.
    result = name.replace("\\", "/").split("/")[-1]
    result = "".join(ch for ch in result if ch not in "\r\n\x00" and ord(ch) >= 32)
    if result in ("", ".", ".."):
        raise TransferNowError("Unsafe/empty filename in transfer")
    return result


def looks_like_html(path):
    if not path.exists() or not path.is_file():
        return False
    with path.open("rb") as fh:
        head = fh.read(256).lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<?xml", b"{\"error\""))


def download(url, file_info, output, overwrite=False):
    expected = file_info["size"]
    output.parent.mkdir(parents=True, exist_ok=True)
    part = output.with_name(output.name + ".part")
    if output.exists() and not overwrite:
        if output.stat().st_size == expected and not looks_like_html(output):
            print(f"Already complete: {output}")
            return
        raise TransferNowError(f"{output} already exists; use --overwrite or move it")
    if overwrite and part.exists():
        part.unlink()
    if part.exists() and looks_like_html(part):
        raise TransferNowError(f"{part} is HTML, not a partial file; remove it")
    if part.exists() and part.stat().st_size > expected:
        raise TransferNowError(f"{part} is larger than the expected file")
    print(f"Downloading {file_info['name']} ({expected:,} bytes) -> {output}")
    cmd = [
        "curl", "--fail", "--location", "--continue-at", "-",
        "--retry", "3", "--retry-delay", "2", "--connect-timeout", "30",
        "--referer", SITE + "/", "--output", str(part), url,
    ]
    try:
        process = subprocess.run(cmd, check=False)
    except FileNotFoundError as exc:
        raise TransferNowError("curl is required; install the curl package") from exc
    if process.returncode:
        raise TransferNowError(f"curl failed (exit {process.returncode}); partial file kept at {part}")
    if looks_like_html(part):
        part.unlink()
        raise TransferNowError("Storage returned HTML, not the requested file")
    actual = part.stat().st_size
    if actual != expected:
        raise TransferNowError(
            f"Size mismatch: received {actual:,}, expected {expected:,}; partial file kept at {part}"
        )
    os.replace(part, output)
    print(f"Saved {output}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Public TransferNow sharing URL or transfer ID")
    parser.add_argument("-d", "--directory", type=Path, default=Path("."),
                        help="Download directory (default: current directory)")
    parser.add_argument("-o", "--output", type=Path,
                        help="Override filename (only when downloading one file)")
    parser.add_argument("--file-id", help="Only download the given file ID")
    parser.add_argument("--list", action="store_true", help="List files without downloading")
    parser.add_argument("--check", action="store_true",
                        help="Resolve signed URLs and check their headers without downloading")
    parser.add_argument("--show-url", action="store_true",
                        help="Print temporary signed URLs (with --check)")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing output")
    args = parser.parse_args(argv)
    if args.show_url and not args.check:
        parser.error("--show-url requires --check")
    try:
        share = parse_share(args.url)
        password = os.getenv("TRANSFERNOW_PASSWORD", "")
        files = select_files(metadata(share, password), args.file_id or share.file_id)
        if args.output and len(files) != 1:
            parser.error("--output requires a single file (use --file-id)")
        for entry in files:
            name = safe_name(entry["name"])
            print(f"{entry['id']}  {entry['size']:,} bytes  {name}")
            if args.list:
                continue
            url = signed_url(share, entry["id"], password)
            length, content_type = probe(url)
            if args.check:
                print(f"  Storage reachable: {content_type or 'unspecified type'}, "
                      f"Content-Length={length or 'unspecified'}")
                if args.show_url:
                    print(f"  {url}")
            else:
                output = args.output or args.directory / name
                download(url, entry, output, overwrite=args.overwrite)
        return 0
    except TransferNowError as exc:
        print(f"transfernow: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
