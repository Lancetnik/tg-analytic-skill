# tg-channel-analyze

A [Claude Code](https://claude.com/claude-code) skill that analyzes a Telegram
channel:

- Scrape posts (text, media, reactions, views, forwards, tags) and the comment
  threads under each.
- Track **outward forwarders** — outside channels that re-share your posts —
  and **inward citations** — channels you forwarded from.
- Build an append-only time-series of per-post metrics on every re-run, so you can watch engagement evolve.
- Pull subscriber growth/churn broken down by acquisition source and views by hour of day for the "best time to post" question.

Backed by Telethon, with a separate read-only SQL CLI for querying the local
SQLite DB (one file per channel).

> This is my personal skill — I built it to manage my own channel,
> [**@fastnewsdev**](https://t.me/fastnewsdev). It's shared here in case the
> patterns are useful to someone else running a Telegram channel

## Install

From any project where you want the skill available to Claude Code, use the
[`skills`](https://dev.to/baltz/sharing-skills-with-npx-2nbc) CLI:

```bash
# List available skills in this repo
npx skills@latest add Lancetnik/tg-analytic-skill --list

# Install into the current project (./.claude/skills/tg-analyze/)
npx skills@latest add Lancetnik/tg-analytic-skill --skill tg-channel-analyze

# Or install globally
npx skills@latest add Lancetnik/tg-analytic-skill --skill tg-channel-analyze --global

# Non-interactive
npx skills@latest add Lancetnik/tg-analytic-skill --skill tg-channel-analyze --yes
```

## First-run setup

The scraping commands need Telegram API credentials. In the install target
directory:

```bash
cd .claude/skills/tg-analyze
echo "TG_API_ID=...
TG_API_HASH=...
TG_PHONE=+..." > .env
```

The first `tg_scrape.py` run prompts for a login code interactively; subsequent
runs reuse `session.session`.

## Usage

Inside Claude Code, ask the skill to analyze a channel:

> Analyze @some_channel — show me the most popular posts.
> Who's forwarding @some_channel? Which channels do they cite most often?
> Refresh metrics on the last 20 posts of @some_channel.

Or run the bundled CLIs directly:

```bash
cd .claude/skills/tg-analyze

# Fast first look — newest 100, skip media downloads
uv run scripts/tg_scrape.py scrape --channel @some_channel --latest 100 --no-media

# Full history (long-running)
uv run scripts/tg_scrape.py scrape --channel @some_channel

# Incremental refresh from a date / from a known post id
uv run scripts/tg_scrape.py scrape --channel @some_channel --offset-date 01-05-2026
uv run scripts/tg_scrape.py scrape --channel @some_channel --offset-id 1234

# Reindex specific posts (refresh metrics / comments)
uv run scripts/tg_scrape.py fetch 103 105 108 --channel @some_channel

# Channel-admin-only: subscriber dynamics and best time to post
uv run scripts/tg_scrape.py subscribers --channel @some_channel
uv run scripts/tg_scrape.py views --channel @some_channel

# Read-only SQL over the per-channel DB (no credentials, no network)
uv run scripts/tg_query.py --channel @some_channel \
  "SELECT p.id, p.link, m.views FROM posts p
   JOIN post_metrics m ON p.id = m.post_id
   ORDER BY m.views DESC LIMIT 10"
```

Every `scrape`/`fetch` run prints a Markdown summary to stdout (top posts by
views and reactions, top tags, outward forwarders and inward citations with
their post ids) — usually you can read the answer off that without dropping
into SQL.

Full command reference lives in [`skills/tg-channel-analyze/SKILL.md`](./skills/tg-channel-analyze/SKILL.md).

## Repository layout

```
skills/
  tg-channel-analyze/
    SKILL.md          Skill instructions (frontmatter + body).
    scripts/
      tg_scrape.py    Telethon-based CLI (scrape, fetch, subscribers, views).
      tg_query.py     Stdlib-only read-only SQL CLI.
    data/             Empty placeholder; user-specific DBs live here at runtime.
```
