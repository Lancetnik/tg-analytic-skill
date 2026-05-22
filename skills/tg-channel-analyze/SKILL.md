---
name: tg-analyze
description: >-
  Analyze a Telegram channel - scrape posts/comments/forwards, track subscriber
  growth and churn by source, and inspect views by hour of day. Use when the
  user wants to understand a Telegram channel's content, engagement, audience
  dynamics, or posting performance. Runs the bundled tg_scrape.py CLI.
---

# Telegram channel analysis

This skill drives two self-contained CLIs in `scripts/`:

- **`tg_scrape.py`** - Telethon-based, talks to Telegram (commands: `scrape`,
  `fetch`, `subscribers`, `views`). Needs API credentials and a session file.
- **`tg_query.py`** - read-only SQL against the per-channel SQLite DB (command:
  `query`). No Telethon, no credentials, no network - just `typer` + stdlib,
  so cold start is much faster.

Dependencies are declared inline (PEP 723), so `uv run` installs them
automatically - no project setup needed. Each command prints a Markdown summary
to **stdout** (read it directly) and most also write full data to files for
deeper inspection.

## Prerequisites (check before first run)

- `uv` must be installed (it resolves each script's inline dependencies).
- For `tg_scrape.py` only: Telegram API credentials must be available as
  `TG_API_ID`, `TG_API_HASH`, `TG_PHONE` - either exported in the environment
  or in a `.env` file in the working directory the command is run from.
- For `tg_scrape.py` only: `session.session` must exist (an authenticated
  Telethon session) in the working directory, or pass `--session-file <path>`.
  If it is missing, the first run prompts for a login code interactively - tell
  the user this must be done by hand once; you cannot complete it.
- `subscribers` and `views` require the account to be an **admin** of the target
  channel, and the channel must be large enough for Telegram to compute stats
  (~500+ subscribers). If not, the command logs a clear error and exits 1.
- `tg_query.py` needs none of the above - it only reads `data/<channel>.db`
  produced by an earlier `scrape`/`fetch`/`subscribers` run.
- Run scrape/fetch/subscribers/views with `uv run scripts/tg_scrape.py ...` and
  query with `uv run scripts/tg_query.py ...` (paths relative to this skill
  directory).

## Commands

### `scrape` - content & engagement

```
uv run scripts/tg_scrape.py scrape --channel @name [options]
```

Fetches posts (text, media, reactions, views, forwards, tags, comments) and the
outer public channels that forwarded them. Prints a summary: post count, date
range, totals/averages, top posts by views and reactions, top tags, top
forwarding channels. Full per-post data is persisted to a **per-channel** SQLite
DB at `data/<channel>.db` (e.g. `data/fastnewsdev.db` - the leading `@` is
stripped). Tables: `posts`, `post_attachments`, `post_metrics` (time-series,
one row per scrape), `post_comments`, `public_channels`, `public_shares`.
Re-running upserts posts/comments/channels and appends a new metrics snapshot.

Useful options:
- `--limit N` - cap messages fetched (omit for the whole channel).
- `--offset-id ID` - start at `ID` and walk **backwards** into history
  (id ≤ ID; 0 = no bound, fetches latest).
- `--offset-date DD-MM-YYYY` - return posts published **after** that date. Use to
  walk **forwards** to pick up new content.
- `--no-comments`, `--no-media`, `--no-channel-info` - skip expensive work when
  the user only needs a quick read. Default is to fetch everything.

For a fast first look at an unfamiliar channel, prefer
`scrape --channel @name --limit 100 --no-media`.

### Incremental refresh (the default pattern for an already-populated DB)

A bare `scrape --limit N` is for **first-time / sampling runs only**. When the
DB already has posts, an incremental refresh must be anchored to the last known
state, otherwise either you re-scrape the same old posts or miss the ones
published since:

1. Read the cursor from the DB:
   ```
   uv run scripts/tg_query.py --channel @name "SELECT MAX(id), MAX(date) FROM posts"
   ```
2. Pull only newer posts by date (Telegram's offset semantics — `--offset-date`
   walks forwards):
   ```
   uv run scripts/tg_scrape.py scrape --channel @name --offset-date DD-MM-YYYY
   ```
   Pass the date of the latest known post; the run will pick up everything
   published after it (and re-snapshot that post's metrics, harmless because
   `post_metrics` is append-only).
3. To **also refresh metrics for a known set of older posts** (e.g. recent
   bangers, navigation pins) without walking history, use `fetch <id ...>` —
   one round-trip, appends a new `post_metrics` row per id.

Never use `--limit N` on an existing DB to "just grab recent stuff" — it returns
the last N posts regardless of what you already have, so it misses anything new
beyond that window and silently re-scrapes the rest.

To scrape a **single post by id**, combine an inclusive `--offset-id` with
`--limit 1`:

To scrape a **single post by id**, combine an inclusive `--offset-id` with
`--limit 1`:

```
uv run scripts/tg_scrape.py scrape --channel @name --offset-id 299 --limit 1
```

This fetches exactly post 299. For a known **set** of ids, prefer the `fetch`
command (below) - it's a single Telegram round-trip instead of iterating.

### `fetch` - specific posts by id

```
uv run scripts/tg_scrape.py fetch 103 105 108 --channel @name [options]
```

Fetches the listed post ids directly (one `get_messages` call), persists them
to `data/<channel>.db` the same way `scrape` does, and prints the same summary.
Missing ids are logged and skipped. Album members are auto-grouped by
`grouped_id` when multiple ids of the same album are passed. Accepts the same
`--no-comments` / `--no-media` / `--no-channel-info` toggles as `scrape`.

Use this to refresh a known post's metrics snapshot (append a new
`post_metrics` row) or to pull a handful of posts without scanning history.

### `subscribers` - audience growth & churn

```
uv run scripts/tg_scrape.py subscribers --channel @name
```

Prints a summary: date range, current total, net change, joins/leaves, daily
averages, best/worst day, and new subscribers broken down by source (URL,
search, channels, groups, etc.). Upserts into the channel's DB
(`data/<channel>.db`) tables `subscribers` (date|total|joins|leaves) and
`subscriber_sources` (date|source|count), keyed by `date`. Repeated runs
accumulate history beyond Telegram's retention window.

### `views` - best time to post

```
uv run scripts/tg_scrape.py views --channel @name
```

Prints views per hour of day (0-23, UTC): analyzed period, peak hours, quietest
hours, and the full 24-hour breakdown. Console output only - no file.

### `query` - ad-hoc SQL against the DB

Lives in **`tg_query.py`** (separate script, no Telethon/credentials needed,
single-purpose so the SQL is the first positional argument - no `query`
subcommand):

```
uv run scripts/tg_query.py --channel @fastnewsdev \
  "SELECT p.id, p.link, m.views FROM posts p JOIN post_metrics m ON p.id = m.post_id ORDER BY m.views DESC LIMIT 10"
```

`--channel` selects which `data/<channel>.db` to query (default
`@fastnewsdev`). The post id is `posts.id` but `post_metrics.post_id` - join
on `p.id = m.post_id`. The DB is opened **read-only** (SQLite `mode=ro`), so
writes are rejected at the engine level - safe to send LLM-generated SQL.
Output is a Markdown table. Use `--limit N` to cap rows (default 100, `0` =
unlimited). Cells are truncated at 200 chars by default - pass `--no-truncate`
when you need the full text of a column (post body, long comments). Use this
when the user asks for data not in the summary (specific post text, comment
threads, metrics over time, subscriber series).

## Database schema (data/&lt;channel&gt;.db)

**One SQLite file per channel** at `data/<channel>.db` (the leading `@` is
stripped from the filename). No `channel` column anywhere - the channel is
implicit in which DB you opened. Dates are ISO-8601 strings. Tables:

### `posts` - one row per post (PK: `id`)
- `id` int - Telegram message id
- `link` text - `https://t.me/<channel>/<id>`
- `date` text - post publish timestamp (ISO)
- `text` text - post body
- `edit_date` text - non-null only if the post was edited
- `reply_to_msg_id` int - if the post is a reply
- `tags` text - JSON array of hashtags (no `#`)
- `grouped_id` int - Telegram album id; non-null means multi-attachment post

### `post_attachments` - one row per media item (PK: `post_id`, `attachment_id`)
- `attachment_id` int - id of the message carrying the media (== `post_id` for single-media posts, different for album members)
- `link` text - `https://t.me/<channel>/<attachment_id>`
- `media_type` text - `photo`, `document`, or other Telethon class name
- `photo_path` text - local path to the downloaded JPEG (null when `--no-media`)

### `post_metrics` - time-series, append-only (PK: `id` autoincrement)
- `post_id` int - FK-style to `posts.id`
- `scrape_date` text - ISO timestamp of the scrape run
- `views`, `forwards`, `reactions`, `stars`, `comments_count`, `public_forwards_count` - int snapshots at `scrape_date`
- Latest values per post: `SELECT ... FROM post_metrics WHERE (post_id, scrape_date) IN (SELECT post_id, MAX(scrape_date) FROM post_metrics GROUP BY post_id)`

### `post_comments` - one row per comment (PK: `post_id`, `id`)
- `id` int - comment message id
- `date`, `text`, `author_id`, `author_name`, `author_username`

### `public_channels` - outer channels that re-shared posts (PK: `link`)
- `link` text - `https://t.me/<username>` or `https://t.me/c/<id>` for private
- `name`, `description`, `subscribers` - filled when `--channel-info` was on
- `last_seen` text - latest scrape that observed this channel

### `public_shares` - M2M post -> forwarder (PK: `post_id`, `forwarder_link`, `msg_link`)
- `forwarder_link` text - FK-style join to `public_channels(link)`
- `msg_link` text - link to the forwarded message in the outer channel
- `first_seen` text - scrape timestamp that first observed this share

### `subscribers` - daily subscriber dynamics (PK: `date`)
- `date` text - YYYY-MM-DD (UTC)
- `total` int - total subscribers at end of day (from growth_graph)
- `joins`, `leaves` int - daily counts (leaves stored as positive int)

### `subscriber_sources` - daily joins broken down by source (PK: `date`, `source`)
- `source` text - human label as Telegram reports it (`URL`, `Search`,
  `Groups`, `Channels`, `Other`, etc.)
- `count` int - joins from this source on this date

### Common joins

- Latest engagement per post:
  `posts p JOIN post_metrics m ON p.id = m.post_id`
- Forwarders for a given post:
  `public_shares s JOIN public_channels c ON s.forwarder_link = c.link WHERE s.post_id = ?`
- Album items for a grouped post: `post_attachments WHERE post_id = ?`

## Interpreting results for the user

- The stdout summaries are pre-computed for analysis - lead with those.
- For anything beyond the summary, prefer the `query` subcommand over raw
  `sqlite3` - it enforces read-only and prints a Markdown table the user can
  read directly.
- Telegram only retains stats from when the channel became eligible; the
  `subscribers`/`views` periods are already the maximum the API offers. To build
  a longer subscriber history, run `subscribers` periodically - the DB upsert
  keeps old rows.
- Channel/limit defaults exist but always pass `--channel` explicitly unless the
  user is clearly working with the repo's default channel.