---
name: tg-analyze
description: >-
  Analyze a Telegram channel - scrape posts/comments/forwards, track subscriber
  growth and churn by source, and inspect views by hour of day. Use when the
  user wants to understand a Telegram channel's content, engagement, audience
  dynamics, or posting performance. Runs the bundled tg_scrape.py CLI.
---

# Telegram channel analysis

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

## Database schema (data/&lt;channel&gt;.db)

One file per channel. No `channel` column anywhere - it's implicit in which DB
you opened. Dates are ISO-8601 strings.

### `posts` (PK: `id`)
- `id` int - Telegram message id
- `link` text - `https://t.me/<channel>/<id>`
- `date` text - publish timestamp
- `text` text - post body
- `edit_date` text - non-null only if edited
- `reply_to_msg_id` int - if the post is a reply
- `tags` text - JSON array of hashtags (no `#`)
- `grouped_id` int - Telegram album id; non-null = multi-attachment post

### `post_attachments` (PK: `post_id`, `attachment_id`)
- `attachment_id` int - == `post_id` for single-media posts; differs for album members
- `link` text - `https://t.me/<channel>/<attachment_id>`
- `media_type` text - `photo`, `document`, or other Telethon class name
- `photo_path` text - local JPEG path (null when `--no-media`)

### `post_metrics` (PK: `id` autoincrement, **append-only time series**)
- `post_id` int - FK-style to `posts.id`
- `scrape_date` text - ISO timestamp of the scrape run
- `views`, `forwards`, `reactions`, `stars`, `comments_count`, `public_forwards_count` int
- Latest per post:
  ```sql
  SELECT ... FROM post_metrics
  WHERE (post_id, scrape_date) IN (
      SELECT post_id, MAX(scrape_date) FROM post_metrics GROUP BY post_id
  )
  ```

### `post_comments` (PK: `post_id`, `id`)
- `id` int - comment message id
- `date`, `text`, `author_id`, `author_name`, `author_username`

### `public_channels` (PK: `link`)
- `link` text - `https://t.me/<username>` or `https://t.me/c/<id>` for private
- `name`, `description`, `subscribers` - filled when `--channel-info` was on
- `last_seen` text - latest scrape that observed this channel

### `public_shares` (PK: `post_id`, `forwarder_link`, `msg_link`)
- `forwarder_link` text - joins to `public_channels(link)`
- `msg_link` text - the forwarded message in the outer channel
- `first_seen` text - scrape timestamp that first observed this share

### `subscribers` (PK: `date`)
- `date` text - YYYY-MM-DD (UTC)
- `total` int - subscribers at end of day
- `joins`, `leaves` int - daily counts (leaves stored as positive)

### `subscriber_sources` (PK: `date`, `source`)
- `source` text - Telegram label (`URL`, `Search`, `Groups`, `Channels`,
  `Other`, etc.)
- `count` int - joins from this source on this date

### Common joins

- Latest engagement per post: `posts p JOIN post_metrics m ON p.id = m.post_id`
  (combine with the latest-per-post predicate above)
- Forwarders for a post: `public_shares s JOIN public_channels c ON s.forwarder_link = c.link WHERE s.post_id = ?`
- Album items: `post_attachments WHERE post_id = ?`

## Interpreting results

- Lead with the stdout summary; it's pre-computed for the most common
  questions.
- For anything beyond it, use the `query` command rather than raw `sqlite3` - it enforces read-only and prints a Markdown table the user can read directly.
- The `subscribers` / `views` periods are already the maximum Telegram offers. To build longer subscriber history, schedule `subscribers` periodically - upserts keep old rows.
