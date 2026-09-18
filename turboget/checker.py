"""
Server capability checks for TurboGet.

Before splitting a download, we need to know:
- Does the server support Range requests? (Accept-Ranges: bytes)
- What is the total file size? (Content-Length)
- What is the filename? (Content-Disposition or URL path)

All checks are done via HEAD request to avoid downloading any data.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, unquote

import aiohttp


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = "TurboGet/1.0 (Multi-WAN Download Manager; +https://github.com/turboget/turboget)"
_HEAD_TIMEOUT = aiohttp.ClientTimeout(connect=15, total=30)
_VERIFY_TIMEOUT = aiohttp.ClientTimeout(connect=15, total=30)


# ---------------------------------------------------------------------------
# File info
# ---------------------------------------------------------------------------

async def get_file_info(url: str, local_ip: Optional[str] = None) -> dict:
    """
    Fetch file metadata via a HEAD request (follows redirects).

    Returns
    -------
    {
        "size":            int | None   — Content-Length in bytes
        "filename":        str          — Best-guess filename
        "supports_ranges": bool         — True if Accept-Ranges: bytes
        "content_type":    str
        "final_url":       str          — URL after redirects
    }
    """
    connector = _make_connector(local_ip)
    headers = {"User-Agent": USER_AGENT}

    try:
        async with aiohttp.ClientSession(
            connector=connector,
            headers=headers,
            timeout=_HEAD_TIMEOUT,
        ) as session:
            async with session.head(url, allow_redirects=True) as resp:
                final_url = str(resp.url)

                # ── file size ──────────────────────────────────────────────
                size: Optional[int] = None
                cl = resp.headers.get("Content-Length")
                if cl:
                    try:
                        size = int(cl)
                    except ValueError:
                        pass

                # ── range support ──────────────────────────────────────────
                supports_ranges = (
                    resp.headers.get("Accept-Ranges", "").strip().lower() == "bytes"
                )

                # ── filename ───────────────────────────────────────────────
                filename = _extract_filename(resp.headers, final_url)

                return {
                    "size": size,
                    "filename": filename,
                    "supports_ranges": supports_ranges,
                    "content_type": resp.headers.get(
                        "Content-Type", "application/octet-stream"
                    ),
                    "final_url": final_url,
                }
    finally:
        await connector.close()


async def verify_range_support(url: str, local_ip: Optional[str] = None) -> bool:
    """
    Confirm range support by actually requesting a 1-byte range.
    Server must reply with HTTP 206 Partial Content.
    """
    connector = _make_connector(local_ip)
    try:
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=_VERIFY_TIMEOUT,
        ) as session:
            async with session.get(
                url,
                headers={"Range": "bytes=0-0", "User-Agent": USER_AGENT},
                allow_redirects=True,
            ) as resp:
                return resp.status == 206
    except Exception:
        return False
    finally:
        await connector.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_connector(local_ip: Optional[str]) -> aiohttp.TCPConnector:
    kwargs: dict = {}
    if local_ip:
        kwargs["local_addr"] = (local_ip, 0)
    return aiohttp.TCPConnector(**kwargs)


def _extract_filename(headers, url: str) -> str:
    """Best-effort filename extraction from headers or URL."""
    # Content-Disposition: attachment; filename="foo.zip"
    # Content-Disposition: attachment; filename*=UTF-8''foo%20bar.zip
    cd = headers.get("Content-Disposition", "")
    if cd:
        # RFC 5987 extended notation (filename*=)
        m = re.search(r"filename\*\s*=\s*(?:[^']*'[^']*')?([^;\s]+)", cd)
        if m:
            return unquote(m.group(1).strip("\"'"))
        # Plain filename=
        m = re.search(r'filename\s*=\s*["\']?([^"\';\r\n]+)', cd)
        if m:
            return unquote(m.group(1).strip("\"' "))

    # Fall back to URL path
    path = urlparse(url).path
    name = Path(unquote(path)).name
    if name and "." in name:
        return name

    return "download"
