#!/usr/bin/env python3
"""
news_provider.py — Free macro / futures news digest.

Pulls headlines from public RSS feeds (no API keys needed) and caches them
for 1 hour to stay polite. Consumed by:
  - web_controller.py  (/api/news-digest)
  - run_autopilot.py   (pre-trade gut-check)

If you later want a paid feed (Benzinga, Polygon News, NewsAPI), just add
another source below.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from xml.etree import ElementTree as ET


NEWS_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_cache.json")
CACHE_TTL_SECONDS = int(os.environ.get("NEWS_DIGEST_TTL", "3600"))
USER_AGENT = "Mozilla/5.0 (FuturesApp/1.0; +futures-app)"

# ─── Sources ───────────────────────────────────────────────────────────
# Free RSS feeds — each one returns a handful of recent headlines.
SOURCES = [
    {
        "name": "macro",
        "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",      # top economy news
        "max": 5,
    },
    {
        "name": "markets",
        "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html",       # markets
        "max": 5,
    },
    {
        "name": "commodities",
        "url": "https://www.cnbc.com/id/19854910/device/rss/rss.html",       # energy / commodities
        "max": 5,
    },
    {
        "name": "yahoo-futures",
        "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=ES=F,NQ=F,GC=F,CL=F&region=US&lang=en-US",
        "max": 5,
    },
]


_lock = threading.Lock()
_memory_cache: dict | None = None
_memory_cache_ts: float = 0.0


# ─── Fetch + parse ─────────────────────────────────────────────────────
def _fetch_rss(url: str, timeout: int = 10) -> list[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as e:
        return [{"title": f"[fetch failed: {e}]", "link": url, "published": ""}]

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    items = []
    # RSS 2.0: <rss><channel><item>
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        date_el = item.find("pubDate")
        desc_el = item.find("description")
        items.append({
            "title": (title_el.text or "").strip() if title_el is not None else "",
            "link": (link_el.text or "").strip() if link_el is not None else "",
            "published": (date_el.text or "").strip() if date_el is not None else "",
            "summary": _strip_html((desc_el.text or "").strip() if desc_el is not None else ""),
        })
    return items


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ─── Digest builder ────────────────────────────────────────────────────
def _build_digest(force: bool = False) -> dict:
    now = time.time()
    global _memory_cache, _memory_cache_ts
    if not force and _memory_cache and (now - _memory_cache_ts) < CACHE_TTL_SECONDS:
        return _memory_cache

    # Try the on-disk cache before hitting the network
    if not force and os.path.exists(NEWS_CACHE_PATH):
        try:
            with open(NEWS_CACHE_PATH) as f:
                cached = json.load(f)
            ts = cached.get("_fetched_at", 0)
            if (now - ts) < CACHE_TTL_SECONDS:
                _memory_cache = cached
                _memory_cache_ts = ts
                return cached
        except Exception:
            pass

    digest: dict = {
        "_fetched_at": now,
        "_fetched_at_iso": datetime.now(timezone.utc).isoformat(),
        "categories": {},
        "all_headlines": [],
    }

    for src in SOURCES:
        items = _fetch_rss(src["url"])[: src["max"]]
        digest["categories"][src["name"]] = items
        digest["all_headlines"].extend(items)

    # Simple plain-English summary
    digest["summary"] = _summarize(digest["categories"])

    try:
        with open(NEWS_CACHE_PATH, "w") as f:
            json.dump(digest, f, indent=2)
    except Exception:
        pass

    _memory_cache = digest
    _memory_cache_ts = now
    return digest


def _summarize(by_cat: dict) -> str:
    lines = []
    for cat, items in by_cat.items():
        if not items:
            continue
        titles = [i["title"] for i in items[:3] if i.get("title")]
        if titles:
            lines.append(f"{cat}: " + "; ".join(titles))
    return " | ".join(lines) if lines else "No recent headlines."


# ─── Public API ────────────────────────────────────────────────────────
def get_digest(force: bool = False) -> dict:
    """Return the cached digest, refreshing if the TTL has expired."""
    with _lock:
        return _build_digest(force=force)


def get_pretrade_check() -> dict:
    """Light-weight pre-trade context: a handful of top headlines + timestamp.

    Used by run_autopilot.py before placing paper trades.
    """
    d = get_digest()
    top = d.get("all_headlines", [])[:6]
    return {
        "as_of": d.get("_fetched_at_iso"),
        "headlines": [{"title": h.get("title"), "link": h.get("link")} for h in top],
        "summary": d.get("summary", ""),
    }


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="Ignore cache")
    ap.add_argument("--pretrade", action="store_true", help="Show only pretrade check")
    args = ap.parse_args()

    if args.pretrade:
        print(json.dumps(get_pretrade_check(), indent=2))
        return
    d = get_digest(force=args.force)
    print(d.get("summary", "(no summary)"))
    print("-" * 55)
    for cat, items in d["categories"].items():
        print(f"\n[{cat}]")
        for i in items:
            print(f"  - {i['title']}")


if __name__ == "__main__":
    main()
