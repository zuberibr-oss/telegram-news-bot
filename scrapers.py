"""
Scrapers for Ethiopian sources that have no usable RSS feed.

Use one in feeds.txt as   scrape:<name> | Label   (e.g. scrape:nbe_directives).

Each scraper takes a requests.Session and returns a list of dicts with
title, link, summary and published (unix seconds). Items without a reliable
date are dropped on purpose: the bot's MAX_AGE_HOURS filter is what stops a
newly added source from posting its whole backlog.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup

TIMEOUT = 20


def clean(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- #
# National Bank of Ethiopia: directives, regulations, proclamations, guidelines
# --------------------------------------------------------------------------- #
# NBE's site is WordPress. Legal documents are a custom "files" post type whose
# RSS feed has no category or text, so read the REST API instead.
NBE_API = "https://nbe.gov.et/wp-json/wp/v2"
NBE_DOC_TYPES = {  # taxonomy -> how to describe it; annual reports, bulletins etc. are skipped
    "directives": "directive",
    "regulations": "regulation",
    "proclamations": "proclamation",
    "guidelines": "guideline",
}


def nbe_directives(session: requests.Session) -> list[dict]:
    resp = session.get(
        f"{NBE_API}/files",
        params={"per_page": 20, "_fields": "date_gmt,title,link," + ",".join(NBE_DOC_TYPES)},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    topics = _nbe_directive_topics(session)

    entries = []
    for post in resp.json():
        kind = next((name for tax, name in NBE_DOC_TYPES.items() if post.get(tax)), None)
        if not kind:
            continue
        title = clean(post["title"]["rendered"])
        topic = ", ".join(topics[t] for t in post.get("directives", []) if t in topics)
        summary = (
            f"The National Bank of Ethiopia has published a new {kind}"
            + (f" in the category '{topic}'" if topic else "")
            + f", titled: {title}. Only the title is available; the full text is in the linked document."
        )
        published = datetime.fromisoformat(post["date_gmt"]).replace(tzinfo=timezone.utc).timestamp()
        entries.append({"title": title, "link": post["link"], "summary": summary, "published": published})
    return entries


def _nbe_directive_topics(session: requests.Session) -> dict[int, str]:
    # Directive categories like "Payment System" or "Foreign Exchange Management".
    # Nice to have, so a failure here must not drop the directives themselves.
    try:
        resp = session.get(f"{NBE_API}/directives", params={"per_page": 100, "_fields": "id,name"}, timeout=TIMEOUT)
        resp.raise_for_status()
        return {t["id"]: clean(t["name"]) for t in resp.json() if t["name"] != "Archives"}
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# National Bank of Ethiopia: announcements (circulars and instructions to banks)
# --------------------------------------------------------------------------- #
NBE_ANNOUNCEMENTS = "https://nbe.gov.et/announcements/"


def nbe_announcements(session: requests.Session) -> list[dict]:
    # A single page with one accordion entry per announcement: title, text and PDF links.
    # The page has no dates, so the first PDF's Last-Modified header stands in for one.
    resp = session.get(NBE_ANNOUNCEMENTS, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    entries = []
    for block in soup.select(".elementor-accordion-item"):
        title_el = block.select_one(".elementor-tab-title")
        body = block.select_one(".elementor-tab-content")
        if not title_el or not body:
            continue
        title = clean(title_el.get_text(" "))
        pdfs = [a["href"] for a in body.find_all("a", href=True) if a["href"].lower().split("?")[0].endswith(".pdf")]
        published = _last_modified(session, pdfs[0]) if pdfs else 0.0
        if not title or not published:
            print(f"[scrape] skipping undated NBE announcement: {title[:60]}", flush=True)
            continue
        entries.append({
            "title": title,
            "link": pdfs[0],
            "summary": clean(body.get_text(" ")),
            "published": published,
        })
    return entries


def _last_modified(session: requests.Session, url: str) -> float:
    try:
        resp = session.head(url, timeout=TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        return parsedate_to_datetime(resp.headers["Last-Modified"]).timestamp()
    except Exception:
        return 0.0


SCRAPERS = {
    "nbe_directives": nbe_directives,
    "nbe_announcements": nbe_announcements,
}


def run(name: str, session: requests.Session) -> list[dict]:
    if name not in SCRAPERS:
        raise ValueError(f"unknown scraper '{name}' (available: {', '.join(SCRAPERS)})")
    return SCRAPERS[name](session)
