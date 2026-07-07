"""Page fetching + main-text extraction.

`url_allowed` is the SSRF guard: search results are attacker-influenced, so a
result URL must never become a probe of the LAN. Hosts are resolved and every
address must be globally routable (`ip.is_global` rejects loopback, private,
link-local, reserved, and multicast ranges).

Fetchers return None on any refusal or failure — a lost source is a skipped
source, never a crashed run."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MAX_RESPONSE_BYTES = 2_000_000
FETCH_TIMEOUT_S = 20
# Firecrawl renders JS server-side before returning markdown; that regularly
# takes longer than a plain GET, so it gets its own budget.
FIRECRAWL_TIMEOUT_S = 60
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"


def url_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def fetch_extract(url: str, char_cap: int) -> str | None:
    """GET the page and return extracted main text, truncated to char_cap.
    None when the url is refused, the request fails, or nothing extracts."""
    if not url_allowed(url):
        logger.info("fetch refused (non-public url): %s", url)
        return None
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=FETCH_TIMEOUT_S,
            stream=True,
        )
        response.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_RESPONSE_BYTES:
                break
        html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
    except requests.RequestException as exc:
        logger.info("fetch failed for %s: %s", url, exc)
        return None
    text = extract_text(html)
    if not text:
        return None
    return text[:char_cap]


def extract_text(html: str) -> str:
    """Main-text extraction via trafilatura, with a tag-stripping fallback
    for pages trafilatura rejects (tiny or malformed documents)."""
    import trafilatura

    text = trafilatura.extract(html)
    if text:
        return text.strip()
    stripped = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def fetch_extract_firecrawl(url: str, char_cap: int) -> str | None:
    """Fetch via Firecrawl's scrape API (handles JS-heavy pages). Same
    contract and SSRF guard as fetch_extract; needs FIRECRAWL_API_KEY."""
    if not url_allowed(url):
        logger.info("fetch refused (non-public url): %s", url)
        return None
    try:
        response = requests.post(
            FIRECRAWL_SCRAPE_URL,
            json={"url": url, "formats": ["markdown"]},
            headers={
                "Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}"
            },
            timeout=FIRECRAWL_TIMEOUT_S,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        logger.info("firecrawl scrape failed for %s: %s", url, exc)
        return None
    markdown = ((payload.get("data") or {}).get("markdown") or "").strip()
    if not markdown:
        return None
    return markdown[:char_cap]
