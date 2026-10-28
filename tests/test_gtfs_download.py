from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest

from fare_dodger.gtfs.download import download_feed, extract_feed, read_meta

URL = "https://example.invalid/gtfs.zip"


def _zip_bytes(extra: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("feed_info.txt", "feed_version,feed_start_date\nv42,20261101\n")
        zf.writestr("stops.txt", "stop_id\nA\n")
        for name, body in (extra or {}).items():
            zf.writestr(name, body)
    return buf.getvalue()


def test_download_then_conditional_304(tmp_path: Path) -> None:
    payload = _zip_bytes()
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        if request.headers.get("if-none-match") == '"abc"':
            return httpx.Response(304)
        return httpx.Response(200, content=payload, headers={"ETag": '"abc"'})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    first = download_feed(URL, tmp_path, client=client)
    assert first.downloaded
    assert first.meta.feed_version == "v42"
    assert first.meta.size_bytes == len(payload)
    assert read_meta(tmp_path) == first.meta

    second = download_feed(URL, tmp_path, client=client)
    assert not second.downloaded
    assert seen[-1]["if-none-match"] == '"abc"'

    forced = download_feed(URL, tmp_path, client=client, force=True)
    assert forced.downloaded
    assert "if-none-match" not in seen[-1]


def test_download_rejects_non_zip(tmp_path: Path) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"<html>"))
    )
    with pytest.raises(ValueError, match="zip"):
        download_feed(URL, tmp_path, client=client)
    assert not (tmp_path / "gtfs.zip").exists()


def test_extract_only_top_level_tables(tmp_path: Path) -> None:
    zip_path = tmp_path / "feed.zip"
    zip_path.write_bytes(_zip_bytes({"../evil.txt": "x", "sub/dir.txt": "x", "notes.md": "x"}))
    out = extract_feed(zip_path, tmp_path / "out")
    assert sorted(p.name for p in out.glob("*.txt")) == ["feed_info.txt", "stops.txt"]
    assert not (tmp_path / "evil.txt").exists()
    assert extract_feed(zip_path, out) == out  # idempotent
