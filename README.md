# tg-analytic-skill

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

# Install into the current project (./.claude/skills/tg-analytic-skill/)
npx skills@latest add Lancetnik/tg-analytic-skill --skill tg-analytic-skill

# Or install globally
npx skills@latest add Lancetnik/tg-analytic-skill --skill tg-analytic-skill --global

# Non-interactive
npx skills@latest add Lancetnik/tg-analytic-skill --skill tg-analytic-skill --yes
```

## First-run setup

The scraping commands need Telegram API credentials
([create them here](https://my.telegram.org/apps)). All runtime state lives
in `.tg-analytic/` at your **project root** (the directory you run commands
from) — never inside the skill. The examples below use `$SKILL` for wherever
the skill landed (`.claude/skills/tg-analytic-skill` or
`.agents/skills/tg-analytic-skill` in a project; `skills/tg-analytic-skill`
in this repo):

```bash
SKILL=.claude/skills/tg-analytic-skill   # adjust to your install
mkdir -p .tg-analytic
cp "$SKILL/.env.example" .tg-analytic/.env
# edit .tg-analytic/.env: TG_API_ID, TG_API_HASH, TG_PHONE
```

If you ask Claude to analyze a channel before `.env` exists, the skill will
prompt you for the three values and write the file for you.

Then authenticate once — **in your own terminal**, not via Claude — so
Telethon can prompt for the SMS code and 2FA password on stdin:

```bash
uv run "$SKILL/scripts/tg_scrape.py" login
```

This writes `session.session`; every later scrape/fetch/subscribers/views
reuses it. If the session file is missing, those commands exit with an
explicit error pointing back at `login`.

## Usage

Inside Claude Code, ask the skill to analyze a channel:

> Analyze @some_channel — show me the most popular posts.
> Who's forwarding @some_channel? Which channels do they cite most often?
> Refresh metrics on the last 20 posts of @some_channel.

Or run the bundled CLIs directly — always from the **project root** (the
scripts anchor `.tg-analytic/` on the current working directory):

```bash
SKILL=.claude/skills/tg-analytic-skill   # adjust to your install

# Fast first look — newest 100, skip media downloads
uv run "$SKILL/scripts/tg_scrape.py" scrape --channel @some_channel --latest 100 --no-media

# Full history (long-running)
uv run "$SKILL/scripts/tg_scrape.py" scrape --channel @some_channel

# Incremental refresh from a date / from a known post id
uv run "$SKILL/scripts/tg_scrape.py" scrape --channel @some_channel --offset-date 01-05-2026
uv run "$SKILL/scripts/tg_scrape.py" scrape --channel @some_channel --offset-id 1234

# Reindex specific posts (refresh metrics / comments)
uv run "$SKILL/scripts/tg_scrape.py" fetch 103 105 108 --channel @some_channel

# Channel-admin-only: subscriber dynamics and best time to post
uv run "$SKILL/scripts/tg_scrape.py" subscribers --channel @some_channel
uv run "$SKILL/scripts/tg_scrape.py" views --channel @some_channel

# Read-only SQL over the per-channel DB (no credentials, no network)
uv run "$SKILL/scripts/tg_query.py" --channel @some_channel \
  "SELECT p.id, p.link, m.views FROM posts p
   JOIN post_metrics m ON p.id = m.post_id
   ORDER BY m.views DESC LIMIT 10"
```

Every `scrape`/`fetch` run prints a Markdown summary to stdout (top posts by
views and reactions, top tags, outward forwarders and inward citations with
their post ids) — usually you can read the answer off that without dropping
into SQL.

Full command reference lives in [`skills/tg-analytic-skill/SKILL.md`](./skills/tg-analytic-skill/SKILL.md).

## Repository layout

```
skills/
  tg-analytic-skill/
    SKILL.md          Skill instructions (frontmatter + body).
    .env.example      Template for the three Telegram credentials.
    scripts/
      tg_scrape.py           Telethon-based CLI (scrape, fetch, subscribers, views).
      tg_query.py            Stdlib-only read-only SQL CLI.
      _common.py             Shared paths, DB schema (source of truth), open helpers.
      _render.py             Markdown renderers for the per-command summaries.
    references/
      schema.md       DB schema reference for writing SQL.
tools/
  check_schema_doc.py  Dev-only: guard SCHEMA <-> references/schema.md drift (not shipped).
```

Runtime state (`.env`, the Telethon session, per-channel `*.db` files, media)
lives in a gitignored `.tg-analytic/` directory at the root of whatever
project you run the skill from — nothing is written inside the skill itself.
