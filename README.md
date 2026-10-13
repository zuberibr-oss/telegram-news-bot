# Amharic News Bot for Telegram

Every 30 minutes, GitHub Actions pulls news feeds, an LLM (Gemini by default, or Claude) decides
what's worth posting and writes an Amharic headline + short summary, and the bot posts it
to your channel with a link to the original article.

One bot runs two channels, each with its own feed list, editorial brief and seen-stories file:

| Profile | Channel | Covers | Feeds | State |
|---|---|---|---|---|
| `tech` | `TELEGRAM_CHANNEL` secret | African/Ethiopian tech, startups, fintech, telecom | `feeds.txt` | `state/seen.json` |
| `main` | `@enxmain` | Ethiopian news + major world news | `feeds_main.txt` | `state/seen_main.json` |

## Setup (about 15 minutes)

### 1. Telegram
1. Create the channel in Telegram.
2. Message **@BotFather** → `/newbot` → copy the token.
3. Channel → Administrators → Add admin → pick your bot → allow **Post messages**.
   Do this in **every** channel the bot posts to (the tech channel and `@enxmain`).
4. Channel ID: use `@yourchannelname` for a public channel. For a private channel, forward a
   channel post to **@userinfobot** (or similar) to get the numeric ID, which starts with `-100`.

### 2. LLM API key
Create a Gemini key at aistudio.google.com (the free tier works; it covers Flash models only, not Pro).
To use Claude instead, create a key at console.anthropic.com and set `ANTHROPIC_API_KEY` without `GEMINI_API_KEY`.

### 3. GitHub
1. Create a new repository (private is fine) and upload all these files, keeping the
   `.github/workflows/` folder structure.
2. **Settings → Secrets and variables → Actions → Secrets**, add:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHANNEL` (e.g. `@yourchannel` or `-1001234567890`)
   - `GEMINI_API_KEY` (or `ANTHROPIC_API_KEY` for Claude)
3. Optional, same page under **Variables**:
   - `MODEL` (comma-separated fallback list; default `gemini-3.5-flash,gemini-2.5-flash,gemini-3.6-flash,gemini-3.7-flash,gemini-3.8-flash`,
     or `claude-sonnet-5-5` when using Claude)
   - `MAX_POSTS_PER_RUN` (default `4`)
   - `CHANNEL_SIGNATURE` (e.g. `@yourchannel`, added to the bottom of each post)
   - For the main channel: `TELEGRAM_CHANNEL_MAIN` (default `@enxmain`), `MAX_POSTS_PER_RUN_MAIN`
     (default `4`) and `CHANNEL_SIGNATURE_MAIN`. To give it its own Gemini quota, add a
     `GEMINI_API_KEY_MAIN` **secret**; without one it shares `GEMINI_API_KEY`.
4. **Settings → Actions → General → Workflow permissions** → select **Read and write**
   (the bot commits `state/seen.json` so it never posts the same story twice).
5. **Actions** tab → **Post news** → **Run workflow**. Open the run log: each feed shows
   how many items it returned and each story shows PUBLISH/skip with a reason.

A channel's first run posts up to 4 of the newest relevant stories and marks the rest as already
seen, so the channel doesn't get flooded.

## Tuning

| Want to... | Change |
|---|---|
| Add/remove sources | `feeds.txt` or `feeds_main.txt` (any RSS/Atom URL, optional `| Label`) |
| Add a site with no RSS | write a function in `scrapers.py`, register it in `SCRAPERS`, add `scrape:<name> | Label` to the feed list |
| Change what counts as on-topic | `TECH_EDITORIAL` or `MAIN_EDITORIAL` in `news_bot.py` |
| Change the writing style (both channels) | `WRITING_RULES` in `news_bot.py` |
| Post more or less often | the `cron` line in `.github/workflows/news.yml` |
| Fewer/more posts per run | `MAX_POSTS_PER_RUN` / `MAX_POSTS_PER_RUN_MAIN` variable |
| Test a feed list | `python news_bot.py --check-feeds` |
| Preview posts without sending | `python news_bot.py --dry-run` (needs only `GEMINI_API_KEY`) |
| Add another channel | a `PROFILES` entry, an `EDITORIAL` brief and a feed list in `news_bot.py`, plus a step in `news.yml` |

`--check-feeds` and `--dry-run` work on the tech channel unless `NEWS_PROFILE=main` is set in the environment.

## Things to know

- **GitHub minutes:** every 30 min ≈ 1,440 runner-minutes/month. That fits a private repo's
  free 2,000 minutes; every 15 min would not. Public repos have no limit.
- **Feeds:** Techpoint Africa and Rest of World were confirmed working. TechCabal, Addis Standard
  and the Google News search block some bots; check the first run's log and delete
  any that show `ERROR` or 0 items. Addis Standard sits behind a Cloudflare check, so the main
  channel also follows it through a Google News search.
- **Main channel:** `feeds_main.txt` mixes Ethiopian outlets (The Reporter in English and Amharic,
  Fana, Capital, Addis Fortune, Ethiopia Observer, Wazema, Addis Insight, BBC Amharic, Addis
  Standard) with BBC World, BBC Africa, France 24 Africa and Al Jazeera. The brief publishes
  Ethiopian news of national interest but world news only when it is major, sends routine tech
  stories to `@enxtech`, and requires neutral wording with contested claims attributed (state media
  is reported as the government's account). Stories older than 24 hours are dropped.
- **Ethiopian sources:** Shega, Ethio Telecom press releases and NBE news are RSS feeds the sites
  don't advertise (see the comments in `feeds.txt`). NBE directives and announcements have no feed,
  so `scrapers.py` reads NBE's WordPress API and announcements page. A scraper that can't date an
  item skips it, so a new source can't flood the channel with old stories. Safaricom Ethiopia has no
  working press page, so it is followed through a Google News search.
- **Copyright:** posts are original Amharic summaries (120-150 words when the full article is
  available, 2-3 sentences otherwise) with a link back to the source. Don't change it to repost or
  translate full articles; that is still copying, and outlets can get the channel taken down.
- **Gemini free tier:** only about 20 requests per model per day, and the newest models often
  return "high demand" errors, so the bot falls back through the `MODEL` list. If every model runs
  out, it stops for that run and picks up the remaining stories next time; nothing is lost. For a
  busy channel, enabling billing on the Google project removes the daily cap.
- **Two channels, one quota:** every new feed item costs one request, and the main channel's feeds
  bring roughly 100 new items a day, about the whole free allowance. On a free key shared by
  both channels, each will fall behind and skip stories. Either enable billing, or create a key in a
  second Google project and save it as the `GEMINI_API_KEY_MAIN` secret so the main channel has its
  own allowance (it will still only get through part of the day's news on the free tier).
- **Full articles:** the model reads the full text when the feed includes it (TechCabal, Ethio
  Telecom, NBE) or the page can be read (Rest of World, Shega). Google News items and PDFs only
  have a short description, so they get the short summary. The log marks each story `[full]` or `[short]`.
- **Quality control:** the model is told not to add facts beyond the text it's given. Skim the channel for the first few days
  and tighten `SYSTEM_PROMPT` if anything slips through.
