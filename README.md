# Amharic Tech News Bot for Telegram

Every 30 minutes, GitHub Actions pulls African/Ethiopian tech news feeds, an LLM (Gemini by default, or Claude) decides
what's worth posting and writes an Amharic headline + short summary, and the bot posts it
to your channel with a link to the original article.

## Setup (about 15 minutes)

### 1. Telegram
1. Create the channel in Telegram.
2. Message **@BotFather** → `/newbot` → copy the token.
3. Channel → Administrators → Add admin → pick your bot → allow **Post messages**.
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
4. **Settings → Actions → General → Workflow permissions** → select **Read and write**
   (the bot commits `state/seen.json` so it never posts the same story twice).
5. **Actions** tab → **Post tech news** → **Run workflow**. Open the run log: each feed shows
   how many items it returned and each story shows PUBLISH/skip with a reason.

The first run posts up to 4 of the newest relevant stories and marks the rest as already seen,
so the channel doesn't get flooded.

## Tuning

| Want to... | Change |
|---|---|
| Add/remove sources | `feeds.txt` (any RSS/Atom URL, optional `| Label`) |
| Add a site with no RSS | write a function in `scrapers.py`, register it in `SCRAPERS`, add `scrape:<name> | Label` to `feeds.txt` |
| Change what counts as on-topic, or the writing style | `SYSTEM_PROMPT` in `news_bot.py` |
| Post more or less often | the `cron` line in `.github/workflows/news.yml` |
| Fewer/more posts per run | `MAX_POSTS_PER_RUN` variable |
| Test a feed list | `python news_bot.py --check-feeds` |
| Preview posts without sending | `python news_bot.py --dry-run` (needs only `GEMINI_API_KEY`) |

## Things to know

- **GitHub minutes:** every 30 min ≈ 1,440 runner-minutes/month. That fits a private repo's
  free 2,000 minutes; every 15 min would not. Public repos have no limit.
- **Feeds:** Techpoint Africa and Rest of World were confirmed working. TechCabal, Addis Standard,
  The Reporter and the Google News search block some bots; check the first run's log and delete
  any that show `ERROR` or 0 items.
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
- **Full articles:** the model reads the full text when the feed includes it (TechCabal, Ethio
  Telecom, NBE) or the page can be read (Rest of World, Shega). Google News items and PDFs only
  have a short description, so they get the short summary. The log marks each story `[full]` or `[short]`.
- **Quality control:** the model is told not to add facts beyond the text it's given. Skim the channel for the first few days
  and tighten `SYSTEM_PROMPT` if anything slips through.
