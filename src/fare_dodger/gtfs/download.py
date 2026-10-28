"""Cached download of a GTFS zip.

The zip is stored with a JSON sidecar holding the HTTP validators (ETag / Last-Modified), so a
re-run sends a conditional request and skips the 60 MB transfer when the feed is unchanged.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

ZIP_NAME = "gtfs.zip"
META_NAME = "gtfs.meta.json"
_CHUNK = 1 << 20


@dataclass(frozen=True, slots=True)
class FeedMeta:
    url: str
    sha256: str
    size_bytes: int
    downloaded_at: str
    etag: str | None
    last_modified: str | None
    feed_version: str | None
    feed_start_date: str | None
    feed_end_date: str | None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    zip_path: Path
    meta: FeedMeta
    #: ``False`` when the server answered 304 Not Modified (or the cache was used offline).
    downloaded: bool


def read_meta(directory: Path) -> FeedMeta | None:
    path = directory / META_NAME
    if not path.is_file():
        return None
    return FeedMeta(**json.loads(path.read_text(encoding="utf-8")))


def _feed_info(zip_path: Path) -> dict[str, str]:
    with zipfile.ZipFile(zip_path) as zf:
        if "feed_info.txt" not in zf.namelist():
            return {}
        text = zf.read("feed_info.txt").decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    return rows[0] if rows else {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def download_feed(
    url: str,
    directory: Path,
    *,
    force: bool = False,
    client: httpx.Client | None = None,
) -> DownloadResult:
    """Download ``url`` into ``directory`` unless the cached copy is still current."""
    directory.mkdir(parents=True, exist_ok=True)
    zip_path = directory / ZIP_NAME
    cached = read_meta(directory) if zip_path.is_file() else None

    headers: dict[str, str] = {}
    if cached is not None and not force and cached.url == url:
        if cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified

    own_client = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0))
    try:
        with http.stream("GET", url, headers=headers) as response:
            if response.status_code == httpx.codes.NOT_MODIFIED and cached is not None:
                return DownloadResult(zip_path, cached, downloaded=False)
            response.raise_for_status()
            tmp_path = zip_path.with_suffix(".part")
            with tmp_path.open("wb") as fh:
                for chunk in response.iter_bytes(_CHUNK):
                    fh.write(chunk)
            etag = response.headers.get("ETag")
            last_modified = response.headers.get("Last-Modified")
    finally:
        if own_client:
            http.close()

    if not zipfile.is_zipfile(tmp_path):
        tmp_path.unlink()
        msg = f"{url} did not return a zip file"
        raise ValueError(msg)
    tmp_path.replace(zip_path)

    info = _feed_info(zip_path)
    meta = FeedMeta(
        url=url,
        sha256=_sha256(zip_path),
        size_bytes=zip_path.stat().st_size,
        downloaded_at=datetime.now(UTC).isoformat(timespec="seconds"),
        etag=etag,
        last_modified=last_modified,
        feed_version=info.get("feed_version") or None,
        feed_start_date=info.get("feed_start_date") or None,
        feed_end_date=info.get("feed_end_date") or None,
    )
    (directory / META_NAME).write_text(
        json.dumps(asdict(meta), indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return DownloadResult(zip_path, meta, downloaded=True)


def extract_feed(zip_path: Path, target: Path) -> Path:
    """Extract the top-level ``*.txt`` tables of a GTFS zip into ``target`` (idempotent).

    Only plain file names are extracted, so a malicious archive cannot write outside
    ``target``.
    """
    marker = target / ".complete"
    if marker.is_file():
        return target
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.endswith(".txt") or "/" in name or "\\" in name or name.startswith("."):
                continue
            with zf.open(name) as src, (target / name).open("wb") as dst:
                while chunk := src.read(_CHUNK):
                    dst.write(chunk)
    marker.write_text("ok\n", encoding="utf-8")
    return target
