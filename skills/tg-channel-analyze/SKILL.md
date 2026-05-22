---
name: tg-channel-analyze
description: >-
  Use this skill when the user wants to analyze a Telegram channel — scrape
  posts, comments, forwards, and per-post engagement over time, or pull
  subscriber growth/churn by source and views by hour of day from Telegram's
  stats API. Covers content, engagement, audience dynamics, and posting
  performance. Do not use for one-off reads of a single message, for private
  chats the logged-in account doesn't admin.
  Runs the bundled tg_scrape.py CLI.
---

# Telegram channel analysis

## When to use

- Use for **channel-level** analytics: content history, engagement over time, audience growth, forwarder networks, posting-time optimization.
- Do **not** use to read a single specific message — call Telethon directly or open the link.
- Do **not** use to read private chats or channels the account doesn't have at least observer access to. `subscribers` and `views` additionally require **admin** rights on a channel large enough for Telegram to compute stats.
- Always confirm the channel handle with the user before the first scrape — a typo silently creates a new empty DB at `data/<typo>.db`.

## Reporting back to the user

Every command prints a Markdown summary block to stdout. Use it as-is:

1. One-line headline (channel, time range, headline metric).
2. Paste the script's stdout summary.
3. If the user asked for depth the summary doesn't cover, follow with a
   `tg_query.py` result (Markdown table — already formatted for chat).

Don't paraphrase the summary; the script already pre-computes the
most-asked questions.

## First-run setup (do this before any scraping)

The `scrape`/`fetch`/`subscribers`/`views` commands need two things in the skill directory: a `.env` with Telegram API credentials, and a `session.session` from a one-time interactive login. The `query` command needs neither.

If `.env` is missing:

1. Ask the user for their `TG_API_ID`, `TG_API_HASH`, and `TG_PHONE` (international format, e.g. `+15551234567`). Point them at https://my.telegram.org/apps to create credentials if they don't have them.
2. Copy `.env.example` to `.env` and fill in the values they provided. Do not commit `.env`.

If `session.session` is missing, the next scrape/fetch/subscribers/views command will exit with an explicit error. When that happens, **stop and tell the user to run**:

```
uv run scripts/tg_scrape.py login
```

**in their own terminal** (not via you) — Telethon prompts on stdin for an SMS code and a 2FA password if enabled, which only works in an interactive TTY. Once it writes `session.session`, re-run the original command.

## CLIs

Two CLIs under `scripts/`:

- **`tg_scrape.py`** - talks to Telegram. Commands: `scrape`, `fetch`,
  `subscribers`, `views`.
- **`tg_query.py`** - read-only SQL against the per-channel SQLite DB at
  `data/<channel>.db` (leading `@` stripped from filename).

Run with `uv run scripts/<script>.py ...`. Always pass `--channel @name`
explicitly. Every command prints a Markdown summary to stdout; lead with that
when reporting to the user, then drop into `tg_query.py` for anything deeper.

`subscribers` and `views` require the account to be an **admin** of the
channel, and the channel must be eligible for Telegram stats (~500+ subs). If not, the command logs a clear error and exits 1.

## Pick the pattern matching the user's intent

All `scrape`/`fetch` runs persist to `data/<channel>.db` and **append** a
`post_metrics` row per post per run, so repeated runs build a time series.
Posts, comments, attachments, and forwarder shares are upserted/replaced.

### 1. Initial channel scrape (full history)

```
uv run scripts/tg_scrape.py scrape --channel @name
```

For a fast first look before committing to a full scrape of an unfamiliar
channel:

```
uv run scripts/tg_scrape.py scrape --channel @name --latest 100 --no-media
```

### 2. N newest posts

```
uv run scripts/tg_scrape.py scrape --channel @name --latest 50
```

`--latest N` iterates newest-first. Use this whenever the user says "most
recent", "latest", "newest", or "last N". Safe to re-run on a schedule -
upserts posts and appends a fresh `post_metrics` snapshot each time.

### 3. All posts from a specific date forward

```
uv run scripts/tg_scrape.py scrape --channel @name --offset-date 01-05-2026
```

Date format `DD-MM-YYYY` or `DD-MM-YYYY HH:MM:SS`. Boundary is **exclusive**
(strictly after). Standard incremental-refresh pattern: read the cursor first,
then pass it in:

```
uv run scripts/tg_query.py --channel @name "SELECT MAX(date) FROM posts"
```

### 4. All posts after a specific post id

```
uv run scripts/tg_scrape.py scrape --channel @name --offset-id 1234
```

Starts at post 1234 **inclusive** and walks forward to the newest post. For a
single post, anchor and cap:

```
uv run scripts/tg_scrape.py scrape --channel @name --offset-id 299 --limit 1
```

For a known set of ids, prefer `fetch` (pattern 5) - one Telegram round-trip.

### 5. Reindex specific posts (refresh metrics / comments / forwarders)

```
uv run scripts/tg_scrape.py fetch 103 105 108 --channel @name
```

Appends a new `post_metrics` row per id; replaces comments/attachments/shares for those posts. Missing ids are logged and skipped. Album members auto-group by `grouped_id`.

Refresh only views/forwards/reactions (cheapest):

```
uv run scripts/tg_scrape.py fetch 103 105 108 --channel @name \
    --no-comments --no-media --no-channel-info
```

To pick ids worth reindexing (e.g. recent bangers), pre-query the DB and pass the ids into `fetch`.

### Anti-pattern

`--limit N` without offsets walks oldest-first and stops after N - on a
populated channel this re-scrapes ancient history, not new content. Use
`--latest N` for newest-first, or `--offset-date` / `--offset-id` to walk
forward from a known cursor. `--limit` is only meaningful paired with one of
those offsets, to bound a forward page.

## Other commands

### `subscribers` - audience growth & churn

```
uv run scripts/tg_scrape.py subscribers --channel @name
```

Prints date range, current total, net change, joins/leaves, daily averages,
best/worst day, and new subscribers broken down by source. Upserts into
`subscribers` (date|total|joins|leaves) and `subscriber_sources`
(date|source|count). Repeated runs accumulate history beyond Telegram's
retention window.

### `views` - best time to post

```
uv run scripts/tg_scrape.py views --channel @name
```

Prints views per hour of day (0-23): peak hours, quietest hours, and the full
24-hour breakdown. Console output only.

Hours are in the **Telegram account's local timezone**, not UTC - that's what
the stats API returns and there's no offset to convert from. When reporting
peak hours to the user, say e.g. "20:00 local time (channel admin's tz)" so
they don't misread it as UTC.

### `query` - ad-hoc SQL

```
uv run scripts/tg_query.py --channel @name \
  "SELECT p.id, p.link, m.views FROM posts p JOIN post_metrics m ON p.id = m.post_id ORDER BY m.views DESC LIMIT 10"
```

Read-only (SQLite `mode=ro`, writes rejected by the engine - safe for
LLM-generated SQL). Output is a Markdown table. `--limit N` caps rows (default
100, `0` = unlimited). `--no-truncate` to see full cell content (post body,
long comments). Use whenever the user asks for data not in the stdout summary.

## Database schema

Read [references/schema.md](references/schema.md) before writing SQL with
`tg_query.py`. It documents every table, primary key, and the common joins
(latest metric per post, outward forwarders, inward citation, album items).

## Validation

After running a command, sanity-check the result before reporting:

- After `scrape` / `fetch` — confirm rows landed:
  ```
  uv run scripts/tg_query.py --channel @name \
    "SELECT COUNT(*) posts, MIN(date) oldest, MAX(date) newest FROM posts"
  ```
  If `posts` is 0, the channel handle or session is wrong, not the scrape.
- After `subscribers` — the stdout summary's `period:` line should match the
  window the user asked for. If empty, the channel is below Telegram's stats
  threshold.
- After `views` — expect 24 hourly buckets in the summary. Fewer means a thin stats window; mention this to the user instead of inventing peaks.

## Common errors

| Symptom (stderr) | Cause | Fix |
| --- | --- | --- |
| `Telegram session not found at session.session` | First run, or session deleted | Tell user to run `uv run scripts/tg_scrape.py login` in their own terminal. Do not try to run it yourself — it needs interactive stdin. |
| `failed to get stats ... you must be an admin of a channel that is large enough` | Account isn't admin, or channel < ~500 subs | Skill cannot do `subscribers`/`views` here. Fall back to `scrape` + `post_metrics` for engagement signals. |
| `no followers graph available` / `no top-hours graph available` | Stats exist but the requested graph is empty | Report to user; no retry helps. |
| New, empty `data/<handle>.db` appeared | Channel handle typo | Confirm the handle with the user; delete the empty DB before re-running. |

Telethon may also surface `FloodWaitError` mid-scrape on very large channels — the script logs and continues per item where possible. If a run aborts, re-run with `--offset-id <last-seen-id>` to resume forward rather than restart.

## Long-running history

`subscribers` and `views` periods are already the maximum Telegram offers. To build longer subscriber history, schedule `subscribers` periodically — upserts keep old rows.
