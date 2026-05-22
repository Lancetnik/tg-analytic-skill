# tg-channel-analyze

A [Claude Code](https://claude.com/claude-code) skill that analyzes a Telegram
channel: scrape posts/comments/forwards, track subscriber growth and churn by
source, and inspect views by hour of day. Backed by Telethon, with a separate
read-only SQL CLI for querying the local SQLite DB.

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

Or run the bundled CLIs directly:

```bash
cd .claude/skills/tg-analyze

# Telegram-touching commands (need credentials)
uv run scripts/tg_scrape.py scrape --channel @some_channel
uv run scripts/tg_scrape.py fetch 103 105 108 --channel @some_channel
uv run scripts/tg_scrape.py subscribers --channel @some_channel
uv run scripts/tg_scrape.py views --channel @some_channel

# Read-only SQL over the per-channel DB (no credentials, no network)
uv run scripts/tg_query.py --channel @some_channel \
  "SELECT id, link FROM posts ORDER BY date DESC LIMIT 5"
```

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
