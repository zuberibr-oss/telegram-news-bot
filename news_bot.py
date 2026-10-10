"""
Automatic Amharic tech-news channel for Telegram.

Each run:
  1. Pulls every feed in feeds.txt (RSS, or a scraper from scrapers.py)
  2. Drops anything already seen, too old, or obviously sponsored
  3. Asks the LLM (Gemini or Claude) whether each item fits the channel, and if so writes an
     Amharic headline + 2-3 sentence summary (it also rejects stories that
     duplicate something posted recently)
  4. Posts up to MAX_POSTS_PER_RUN items to the channel
  5. Saves what it has seen to state/seen.json

Environment variables:
  TELEGRAM_BOT_TOKEN   required  token from @BotFather
  TELEGRAM_CHANNEL     required  "@yourchannel" or numeric id like -1001234567890
  GEMINI_API_KEY       required  unless ANTHROPIC_API_KEY is set; Gemini is used when present
  ANTHROPIC_API_KEY    optional  use Claude instead (only when GEMINI_API_KEY is unset)
  MODEL                optional  comma-separated fallback list; see DEFAULT_MODELS
  MAX_POSTS_PER_RUN    optional  default 4
  MAX_LLM_CALLS        optional  default 20 (cap on items evaluated per run)
  MAX_AGE_HOURS        optional  default 36 (ignore older items)
  CHANNEL_SIGNATURE    optional  e.g. "@yourchannel" appended to each post

Usage:
  python news_bot.py              # normal run
  python news_bot.py --dry-run    # print posts instead of sending, don't save state
  python news_bot.py --check-feeds
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import certifi
import feedparser
import requests
from bs4 import BeautifulSoup

import scrapers

ROOT = Path(__file__).resolve().parent
FEEDS_FILE = ROOT / "feeds.txt"
STATE_FILE = ROOT / "state" / "seen.json"
EXTRA_CERTS_FILE = ROOT / "certs" / "intermediates.pem"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
PROVIDER = "gemini" if GEMINI_API_KEY else "anthropic"
# Comma-separated fallback list. Gemini's free tier has a small per-model daily quota and the
# newest models often return 503 under load, so later models take over when earlier ones can't.
DEFAULT_MODELS = {"gemini": "gemini-3.5-flash,gemini-2.5-flash,gemini-3.6-flash,gemini-3.7-flash,gemini-3.8-flash", "anthropic": "claude-sonnet-5-5"}
MODELS = [m.strip() for m in (os.getenv("MODEL") or DEFAULT_MODELS[PROVIDER]).split(",") if m.strip()]
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "4"))
MAX_LLM_CALLS = int(os.getenv("MAX_LLM_CALLS", "20"))
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS", "36"))
MIN_ARTICLE_CHARS = 400  # shorter "full text" is really just a teaser
MAX_ARTICLE_CHARS = 8000  # enough for the LLM to write a full summary; caps tokens per call
SEEN_RETENTION_DAYS = 14
RECENT_HEADLINES_KEPT = 40
USER_AGENT = "Mozilla/5.0 (compatible; TechNewsBot/1.0; +https://t.me/)"

# Cheap pre-filter so we don't pay the LLM to reject obvious paid content.
BLOCKED_URL_PATTERNS = ["/brandpress/", "/sponsored", "/press-release", "/partner-content"]


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# Feeds
# --------------------------------------------------------------------------- #
@dataclass
class Item:
    uid: str
    title: str
    link: str
    summary: str
    source: str
    published: float  # unix seconds; 0 if unknown
    article: str = ""  # full text when the feed or page provides it; the LLM summarizes from this


def load_feeds() -> list[tuple[str, str]]:
    feeds = []
    for raw in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        url, _, label = (part.strip() for part in line.partition("|"))
        feeds.append((url, label))
    return feeds


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_title(title: str) -> str:
    # \w is Unicode-aware, so Amharic headlines keep their letters instead of hashing to "".
    return re.sub(r"[\W_]+", " ", title.lower()).strip()


def make_uid(link: str, title: str) -> str:
    # Hash the normalized title too, so the same headline from two feeds dedupes.
    key = normalize_title(title) or link
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def parse_feed(content: bytes, label: str) -> list[Item]:
    parsed = feedparser.parse(content)
    source = label or strip_html(parsed.feed.get("title", "")) or "Source"
    items = []
    for e in parsed.entries:
        title = strip_html(e.get("title", ""))
        link = e.get("link", "")
        if not title or not link:
            continue
        ts = e.get("published_parsed") or e.get("updated_parsed")
        published = float(calendar.timegm(ts)) if ts else 0.0
        summary = strip_html(e.get("summary", "") or e.get("description", ""))
        # Many WordPress feeds (TechCabal, Ethio Telecom, NBE) carry the whole post in content:encoded.
        content = strip_html(e["content"][0].get("value", "")) if e.get("content") else ""
        article = content if len(content) >= MIN_ARTICLE_CHARS else ""
        # Google News titles end in " - Outlet"; use the outlet as the source.
        item_source = source
        if "news.google.com" in link and " - " in title:
            title, _, outlet = title.rpartition(" - ")
            item_source = outlet.strip() or source
        items.append(Item(make_uid(link, title), title, link, summary[:1500], item_source, published, article))
    return items


ARTICLE_JUNK = re.compile(r"related|share|social|newsletter|subscribe|comment|advert|promo|sidebar|author-bio|breadcrumb|cookie", re.I)
ARTICLE_CONTAINERS = ["[itemprop=articleBody]", ".entry-content", ".post-content", ".article-content", ".article-body", "article", "main"]


def fetch_article_text(session: requests.Session, url: str) -> str:
    """Best-effort main text of an article page; "" if it can't be found."""
    if "news.google.com" in url:
        return ""  # Google News links are JavaScript redirects, not the article
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
    except Exception:
        return ""
    if "html" not in resp.headers.get("Content-Type", ""):
        return ""  # PDFs etc.
    soup = BeautifulSoup(resp.content, "html.parser")

    # Structured data is the cleanest source when present (Shega has it).
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            stack = [json.loads(script.string or "")]
        except ValueError:
            continue
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                body = node.get("articleBody")
                if isinstance(body, str) and len(body) >= MIN_ARTICLE_CHARS:
                    return strip_html(body)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

    for tag in soup(["script", "style", "noscript", "nav", "aside", "footer", "header", "form", "figure", "iframe", "svg"]):
        tag.decompose()
    for tag in soup.find_all(class_=ARTICLE_JUNK):
        tag.decompose()
    for selector in ARTICLE_CONTAINERS:
        for container in soup.select(selector):
            paragraphs = [p.get_text(" ", strip=True) for p in container.find_all(["p", "li"])]
            text = "\n".join(p for p in paragraphs if len(p) > 40)
            if len(text) >= MIN_ARTICLE_CHARS:
                return text
    return ""


def ca_bundle() -> str:
    # Some sources (ethiotelecom.et) don't send their intermediate certificate, which
    # browsers paper over but requests doesn't. Trust certifi's roots plus those intermediates.
    path = Path(tempfile.gettempdir()) / "news_bot_ca_bundle.pem"
    path.write_text(Path(certifi.where()).read_text() + "\n" + EXTRA_CERTS_FILE.read_text())
    return str(path)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.verify = ca_bundle()
    return session


def fetch_feed(url: str, label: str, session: requests.Session) -> list[Item]:
    if url.startswith("scrape:"):
        source = label or url
        return [
            Item(make_uid(e["link"], e["title"]), e["title"], e["link"], e["summary"][:1500], source, e["published"])
            for e in scrapers.run(url.removeprefix("scrape:"), session)
        ]
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    return parse_feed(resp.content, label)


def fetch_all(session: requests.Session) -> list[Item]:
    all_items: list[Item] = []
    for url, label in load_feeds():
        try:
            items = fetch_feed(url, label, session)
            log(f"[feed] {len(items):3d} items  {label or url}")
            all_items.extend(items)
        except Exception as exc:  # one broken feed must not kill the run
            log(f"[feed]   ERROR {label or url}: {exc}")
    return all_items


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    cutoff = time.time() - SEEN_RETENTION_DAYS * 86400
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    state["recent_headlines"] = state["recent_headlines"][-RECENT_HEADLINES_KEPT:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------- #
# LLM (Gemini or Claude): relevance filter + Amharic summary
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """You are the editor of an Amharic-language Telegram channel covering technology, \
startups, fintech, telecom and digital policy for Ethiopian readers.

For each story you receive, decide whether to publish it and, if so, write the post.

PUBLISH stories about: Ethiopian tech, startups, fintech, mobile money (Telebirr, M-Pesa, CBE Birr, \
Chapa etc.), telecom (Ethio Telecom, Safaricom Ethiopia), banking technology, digital ID (Fayda), \
e-commerce, AI, internet access, tech regulation and investment in Ethiopia or Africa, and major global \
tech news that matters to African builders or users.

DO NOT PUBLISH: sponsored posts, press releases dressed as news, crypto price/token promotion, \
generic politics, crime, sports, entertainment, or anything without a clear technology/business angle. \
Also reject any story that covers the same event as one of the recently published headlines provided.

OFFICIAL SOURCES: items from the National Bank of Ethiopia, Ethio Telecom or Safaricom are first-party \
announcements, not disguised press releases. Publish them when they are real news for the sector: new \
directives or rules, licensing, FX or payment-system changes, product and service launches, pricing, \
substantive partnerships, financial results. Skip the routine ones: courtesy visits, CSR and tree planting, \
awards, recruitment, tenders and procurement notices. Some regulator items include only a document title; \
then report only what the title states and don't guess at what the rule says.

WRITING RULES (when publishing):
- Natural, fluent Amharic as an Ethiopian journalist would write it, not a word-for-word translation.
- Keep company, product and person names in their usual Latin spelling (e.g. Safaricom, Telebirr, OpenAI).
- Use Western digits for numbers and keep currencies explicit (ብር, USD).
- Headline: at most 12 words.
- Summary when the full article is provided: 120-150 words in two short paragraphs. First what happened, \
with the key facts, names and figures; then the context or significance the article gives. Separate the \
paragraphs with a blank line.
- Summary when only a short description is provided: 2-3 sentences, at most 60 words.
- Write in your own words. Never translate the article sentence by sentence or reproduce more than a \
short phrase of it; readers follow the link for the full story.
- Use only facts in the provided text. Never invent figures, dates or quotes.
- If the story is outside Ethiopia, make the location clear in the summary."""

DECISION_TOOL = {
    "name": "publish_decision",
    "description": "Record whether to publish this story and the Amharic post text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "publish": {"type": "boolean"},
            "reason": {"type": "string", "description": "One short English sentence explaining the decision."},
            "headline_am": {"type": "string", "description": "Amharic headline. Empty if not publishing."},
            "summary_am": {
                "type": "string",
                "description": "Amharic summary, length per the writing rules. Empty if not publishing.",
            },
            "hashtags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1-3 short English hashtags without '#', e.g. Fintech, Ethiopia, AI.",
            },
        },
        "required": ["publish", "reason", "headline_am", "summary_am", "hashtags"],
    },
}


class QuotaExhausted(RuntimeError):
    """The provider's daily quota is used up, so retrying in this run is pointless."""


exhausted_models: set[str] = set()  # Gemini models out of daily quota during this run


def ask_claude(client, user: str) -> dict:
    resp = client.messages.create(
        model=MODELS[0],
        max_tokens=800,
        system=SYSTEM_PROMPT,
        tools=[DECISION_TOOL],
        tool_choice={"type": "tool", "name": "publish_decision"},
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("model returned no decision")


def ask_gemini(user: str) -> dict:
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": DECISION_TOOL["input_schema"],
        },
    }
    failures = []
    for model in [m for m in MODELS if m not in exhausted_models]:
        r = requests.post(GEMINI_URL.format(model=model), headers={"x-goog-api-key": GEMINI_API_KEY}, json=body, timeout=180)
        if r.status_code == 200:
            candidate = (r.json().get("candidates") or [{}])[0]
            parts = candidate.get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            if not text:
                raise RuntimeError(f"{model} returned no decision (finishReason {candidate.get('finishReason')})")
            return json.loads(text)
        # Daily quota used up (or none at all, e.g. Pro on the free tier): skip this model for the run.
        if r.status_code == 429 and ("PerDay" in r.text or "limit: 0" in r.text):
            exhausted_models.add(model)
            log(f"[llm] {model}: daily quota used up, falling back to the next model")
        # Anything else (503 overload, per-minute limit) just moves on to the next model.
        failures.append(f"{model} {r.status_code}")
    if all(m in exhausted_models for m in MODELS):
        raise QuotaExhausted(f"every model is out of daily quota ({', '.join(MODELS)})")
    raise RuntimeError("Gemini unavailable: " + ", ".join(failures))


def evaluate(client, item: Item, recent_headlines: list[str]) -> dict:
    recent = "\n".join(f"- {h}" for h in recent_headlines[-RECENT_HEADLINES_KEPT:]) or "(none yet)"
    user = (
        f"Recently published headlines (avoid duplicates):\n{recent}\n\n"
        f"NEW STORY\nSource: {item.source}\nTitle: {item.title}\n"
        f"Text: {item.summary or '(no description provided)'}"
    )
    if item.article:
        user += f"\n\nFULL ARTICLE (source material; write the summary in your own words):\n{item.article[:MAX_ARTICLE_CHARS]}"
    else:
        user += "\n\n(Only the short description is available; write the short summary.)"
    for attempt in range(3):
        try:
            return ask_gemini(user) if PROVIDER == "gemini" else ask_claude(client, user)
        except QuotaExhausted:
            raise
        except Exception as exc:
            if attempt == 2:
                raise
            log(f"[llm] retrying after error: {exc}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def format_post(item: Item, decision: dict) -> str:
    esc = html.escape
    tags = " ".join("#" + re.sub(r"[^\w]", "", t) for t in decision.get("hashtags", []) if t.strip())
    parts = [
        f"<b>{esc(decision['headline_am'].strip())}</b>",
        "",
        esc(decision["summary_am"].strip()),
        "",
        f'🔗 <a href="{esc(item.link, quote=True)}">{esc(item.source)}</a>',
    ]
    if tags:
        parts.append(tags)
    signature = os.getenv("CHANNEL_SIGNATURE", "").strip()
    if signature:
        parts += ["", esc(signature)]
    return "\n".join(parts)


def send_telegram(text: str, link: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHANNEL"]
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"url": link, "prefer_large_media": True, "show_above_text": False},
    }
    for _ in range(4):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=20)
        data = r.json()
        if data.get("ok"):
            return
        retry_after = data.get("parameters", {}).get("retry_after")
        if r.status_code == 429 and retry_after:
            log(f"[tg] rate limited, waiting {retry_after}s")
            time.sleep(int(retry_after) + 1)
            continue
        raise RuntimeError(f"Telegram error {r.status_code}: {data.get('description')}")
    raise RuntimeError("Telegram: gave up after repeated rate limits")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(dry_run: bool) -> int:
    state = load_state()
    first_run = state is None
    if first_run:
        state = {"seen": {}, "recent_headlines": []}
        log("[state] first run: will post a few of the newest stories and mark the backlog as seen")

    now = time.time()
    session = make_session()
    items = fetch_all(session)

    # Dedupe within this batch, drop seen / blocked / too old.
    candidates: dict[str, Item] = {}
    for it in items:
        if it.uid in state["seen"] or it.uid in candidates:
            continue
        if any(p in it.link.lower() for p in BLOCKED_URL_PATTERNS):
            state["seen"][it.uid] = now
            continue
        # Undated items are dropped too: "seen" entries expire after SEEN_RETENTION_DAYS, and without
        # a date the age check couldn't stop an old story from being posted again after that.
        if not it.published or now - it.published > MAX_AGE_HOURS * 3600:
            state["seen"][it.uid] = now
            continue
        candidates[it.uid] = it

    # Newest first for evaluation.
    queue = sorted(candidates.values(), key=lambda i: i.published, reverse=True)
    log(f"[run] {len(queue)} new candidate(s)")

    if first_run:
        # Evaluate only the newest few; everything else counts as backlog.
        keep = queue[: MAX_LLM_CALLS]
        for it in queue[MAX_LLM_CALLS:]:
            state["seen"][it.uid] = now
        queue = keep

    if not queue:
        if not dry_run:
            save_state(state)
        return 0

    client = None
    if PROVIDER == "anthropic":
        import anthropic  # imported late so --check-feeds and Gemini runs work without it

        client = anthropic.Anthropic()
    log(f"[llm] using {PROVIDER}: {', '.join(MODELS)}")
    approved: list[tuple[Item, dict]] = []
    llm_calls = 0

    for it in queue:
        if len(approved) >= MAX_POSTS_PER_RUN or llm_calls >= MAX_LLM_CALLS:
            break  # leave the rest unseen for the next run
        llm_calls += 1
        if not it.article:
            it.article = fetch_article_text(session, it.link)
        try:
            decision = evaluate(client, it, state["recent_headlines"] + [a[1]["headline_am"] for a in approved])
        except QuotaExhausted as exc:
            log(f"[llm] {exc}; stopping here, the rest waits for the next run")
            break
        except Exception as exc:
            log(f"[llm] ERROR on '{it.title[:60]}': {exc}  (will retry next run)")
            continue
        verdict = "PUBLISH" if decision.get("publish") else "skip   "
        basis = "full " if it.article else "short"
        log(f"[llm] {verdict} [{basis}] {it.source}: {it.title[:70]}  -- {decision.get('reason', '')}")
        if decision.get("publish") and decision.get("headline_am") and decision.get("summary_am"):
            approved.append((it, decision))
        else:
            state["seen"][it.uid] = now

    if first_run:
        # Mark anything we didn't get to as seen so we don't flood next run.
        for it in queue:
            state["seen"].setdefault(it.uid, now)

    # Post oldest first so the channel reads chronologically.
    approved.sort(key=lambda pair: pair[0].published)
    posted = 0
    for it, decision in approved:
        text = format_post(it, decision)
        if dry_run:
            log("\n" + "=" * 60 + "\n" + text + "\n" + "=" * 60)
        else:
            try:
                send_telegram(text, it.link)
            except Exception as exc:
                log(f"[tg] ERROR posting '{it.title[:60]}': {exc}")
                continue
            time.sleep(3)
        posted += 1
        state["seen"][it.uid] = now
        state["recent_headlines"].append(f"{decision['headline_am']} ({it.title})")
        if not dry_run:
            save_state(state)  # save after every post so a crash can't double-post

    if not dry_run:
        save_state(state)
    log(f"[run] done: {posted} posted, {llm_calls} evaluated")
    return 0


def check_feeds() -> int:
    bad = 0
    session = make_session()
    for url, label in load_feeds():
        try:
            items = fetch_feed(url, label, session)
            newest = max((i.published for i in items), default=0)
            age = f"{(time.time() - newest) / 3600:.0f}h ago" if newest else "no dates"
            log(f"OK   {len(items):3d} items, newest {age:>10}  {label or url}")
            if not items:
                bad += 1
        except Exception as exc:
            bad += 1
            log(f"FAIL {label or url}: {exc}")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print posts instead of sending; don't save state")
    ap.add_argument("--check-feeds", action="store_true", help="test every feed and exit")
    args = ap.parse_args()
    if args.check_feeds:
        return check_feeds()
    missing = [v for v in (() if args.dry_run else ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL")) if not os.getenv(v)]
    if not (GEMINI_API_KEY or os.getenv("ANTHROPIC_API_KEY")):
        missing.insert(0, "GEMINI_API_KEY (or ANTHROPIC_API_KEY)")
    if missing:
        log(f"Missing environment variables: {', '.join(missing)}")
        return 2
    return run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
