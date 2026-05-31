# Database schema (`.tg-analytic/<channel>.db`)

Read this before writing SQL through `tg_query.py`. One SQLite file per channel — leading `@` is stripped from the filename. **There is no `channel` column anywhere** — the channel is implicit in which DB you opened. Don't `WHERE channel = ...`.

The literal `CREATE TABLE` statements below are the source of truth — every column the agent can SELECT, JOIN, or filter on appears there. Notes underneath each table cover only what the DDL can't convey (storage format, semantics, gotchas).

## Full schema at a glance

```sql
CREATE TABLE posts (
    id                     INTEGER PRIMARY KEY,
    link                   TEXT,
    date                   TEXT,
    text                   TEXT,
    edit_date              TEXT,
    reply_to_msg_id        INTEGER,
    tags                   TEXT,
    grouped_id             INTEGER,
    forwarder_from_channel TEXT
);

CREATE TABLE post_attachments (
    post_id        INTEGER NOT NULL,
    attachment_id  INTEGER NOT NULL,
    link           TEXT,
    media_type     TEXT,
    photo_path     TEXT,
    PRIMARY KEY (post_id, attachment_id)
);

CREATE TABLE post_metrics (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id               INTEGER NOT NULL,
    scrape_date           TEXT    NOT NULL,
    views                 INTEGER,
    forwards              INTEGER,
    reactions             INTEGER,
    stars                 INTEGER,
    comments_count        INTEGER,
    public_forwards_count INTEGER
);
CREATE INDEX idx_post_metrics_post ON post_metrics(post_id);

CREATE TABLE post_comments (
    post_id          INTEGER NOT NULL,
    id               INTEGER NOT NULL,
    date             TEXT,
    text             TEXT,
    user_id          INTEGER,
    user_name        TEXT,
    user_username    TEXT,
    PRIMARY KEY (post_id, id)
);

CREATE TABLE public_channels (
    link         TEXT PRIMARY KEY,
    name         TEXT,
    description  TEXT,
    subscribers  INTEGER,
    last_seen    TEXT
);

CREATE TABLE public_shares (
    post_id         INTEGER NOT NULL,
    forwarder_link  TEXT    NOT NULL,
    msg_link        TEXT    NOT NULL,
    first_seen      TEXT,
    PRIMARY KEY (post_id, forwarder_link, msg_link)
);

CREATE TABLE subscribers (
    date     TEXT PRIMARY KEY,
    total    INTEGER,
    joins    INTEGER,
    leaves   INTEGER
);

CREATE TABLE subscriber_sources (
    date     TEXT    NOT NULL,
    source   TEXT    NOT NULL,
    joins    INTEGER,
    PRIMARY KEY (date, source)
);
```

Date/time columns are ISO-8601 strings throughout (`posts.date`, `posts.edit_date`, `post_comments.date`, `public_channels.last_seen`, `public_shares.first_seen`, `post_metrics.scrape_date`). Use SQLite's `date()`, `datetime()`, `strftime()` directly — no conversion needed.

## `posts`

```sql
CREATE TABLE posts (
    id                     INTEGER PRIMARY KEY,
    link                   TEXT,
    date                   TEXT,
    text                   TEXT,
    edit_date              TEXT,
    reply_to_msg_id        INTEGER,
    tags                   TEXT,
    grouped_id             INTEGER,
    forwarder_from_channel TEXT
);
```

- `id` — Telegram message id. Stable across scrapes.
- `link` — `https://t.me/<channel>/<id>`.
- `date`, `edit_date` — ISO-8601 with timezone, e.g. `2026-04-07T08:38:01+00:00`. `edit_date` is non-null only if the post was edited after publish.
- `text` — post body, plain string. Empty for pure-media posts.
- `reply_to_msg_id` — non-null only if the post is a reply (rare on broadcast channels).
- `tags` — **JSON array of hashtag strings without the `#`**, e.g. `["AI", "claude", "cursor"]`. Empty posts store `[]`. Query with `json_each(tags)` to explode, or `tags LIKE '%"AI"%'` for a quick contains check.
- `grouped_id` — Telegram album id. Non-null means the post is part of a multi-attachment album; its members live in `post_attachments`.
- `forwarder_from_channel` — if this post is a forward of another channel's post, the source channel link (joins `public_channels.link`); NULL otherwise. The source channel is auto-inserted into `public_channels`.

## `post_attachments`

```sql
CREATE TABLE post_attachments (
    post_id        INTEGER NOT NULL,
    attachment_id  INTEGER NOT NULL,
    link           TEXT,
    media_type     TEXT,
    photo_path     TEXT,
    PRIMARY KEY (post_id, attachment_id)
);
```

- `post_id` — FK to `posts.id` (the album's representative post).
- `attachment_id` — `== post_id` for single-media posts; **differs** for album members (each album item is a separate Telegram message).
- `link` — `https://t.me/<channel>/<attachment_id>`.
- `media_type` — Telethon class name. Common values: `photo`, `document`, `MessageMediaWebPage`, `MessageMediaPoll`, etc. Filter explicitly by the value you care about — don't assume only `photo`/`document` exist.
- `photo_path` — local JPEG path for photos; NULL when scraped with `--no-media` or for non-photo media.

## `post_metrics` — append-only time series

```sql
CREATE TABLE post_metrics (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id               INTEGER NOT NULL,
    scrape_date           TEXT    NOT NULL,
    views                 INTEGER,
    forwards              INTEGER,
    reactions             INTEGER,
    stars                 INTEGER,
    comments_count        INTEGER,
    public_forwards_count INTEGER
);
```

- `id` — autoincrement surrogate. Strictly increasing — newer rows always have higher `id` than older rows, even within the same `scrape_date` second. **Use `MAX(id)` for "latest", not `MAX(scrape_date)`.**
- `post_id` — FK to `posts.id`. Indexed (`idx_post_metrics_post`).
- `scrape_date` — ISO-8601 with microseconds, e.g. `2026-05-28T19:14:13.024294+00:00`. One row per `(post_id, scrape run)`; the same post gets new rows over time.
- `stars` — paid reactions count (Telegram Stars). Separate from `reactions`.
- `public_forwards_count` — count of public channels that re-shared this post (from Telegram's stats API; requires admin to populate). Details in `public_shares`.

**Canonical "latest snapshot per post"** (use this whenever you join to `posts`):

```sql
WITH latest AS (
    SELECT MAX(id) AS id FROM post_metrics GROUP BY post_id
)
SELECT p.id, p.link, m.views, m.reactions, m.forwards, m.comments_count
FROM posts p
JOIN post_metrics m ON m.id IN (SELECT id FROM latest) AND m.post_id = p.id;
```

For engagement over time on a single post: `SELECT scrape_date, views, reactions FROM post_metrics WHERE post_id = ? ORDER BY id`.

## `post_comments`

```sql
CREATE TABLE post_comments (
    post_id          INTEGER NOT NULL,
    id               INTEGER NOT NULL,
    date             TEXT,
    text             TEXT,
    user_id          INTEGER,
    user_name        TEXT,
    user_username    TEXT,
    PRIMARY KEY (post_id, id)
);
```

- `post_id` — FK to `posts.id`.
- `id` — comment message id in the linked discussion group. Unique only within `post_id`.
- `user_id` — Telegram id of the commenter. When a comment was posted *as a channel* (Telegram's "send as" feature), this is the channel's id and `user_name`/`user_username` carry the channel's title/username.
- `user_username` — without the leading `@`; NULL if the commenter has no public username.
- `user_name` — display name; may be NULL or anonymized.

## `public_channels`

```sql
CREATE TABLE public_channels (
    link         TEXT PRIMARY KEY,
    name         TEXT,
    description  TEXT,
    subscribers  INTEGER,
    last_seen    TEXT
);
```

- `link` — `https://t.me/<username>` (public) or `https://t.me/c/<channel_id>` (private/restricted). Joins to `posts.forwarder_from_channel` (inward) and `public_shares.forwarder_link` (outward).
- `name`, `description`, `subscribers` — populated only when the scrape ran with `--channel-info` (default on). Older rows without channel-info data have these as NULL.
- `last_seen` — ISO-8601 timestamp of the most recent scrape that observed this channel.

## `public_shares` — outbound forward map

```sql
CREATE TABLE public_shares (
    post_id         INTEGER NOT NULL,
    forwarder_link  TEXT    NOT NULL,
    msg_link        TEXT    NOT NULL,
    first_seen      TEXT,
    PRIMARY KEY (post_id, forwarder_link, msg_link)
);
```

- `post_id` — FK to `posts.id` — **one of OUR posts** that got re-shared.
- `forwarder_link` — channel that re-shared us; joins to `public_channels.link`.
- `msg_link` — the forwarded copy in the outer channel (`https://t.me/<their-channel>/<their-msg-id>`).
- `first_seen` — ISO-8601 timestamp of the scrape that first observed this share. Updated on first insert only — re-scrapes don't move it forward.

## `subscribers`

```sql
CREATE TABLE subscribers (
    date     TEXT PRIMARY KEY,
    total    INTEGER,
    joins    INTEGER,
    leaves   INTEGER
);
```

- `date` — `YYYY-MM-DD` (UTC date only, no time).
- `total` — subscribers at end of day.
- `joins`, `leaves` — daily counts. **`leaves` is stored as a positive number** (not negative). Net change = `joins - leaves`.

## `subscriber_sources`

```sql
CREATE TABLE subscriber_sources (
    date     TEXT    NOT NULL,
    source   TEXT    NOT NULL,
    joins    INTEGER,
    PRIMARY KEY (date, source)
);
```

- `date` — `YYYY-MM-DD` (UTC).
- `source` — Telegram-supplied label. Observed values: `URL`, `Search`, `Groups`, `Channels`, `Other`. Don't assume the set is closed.
- `joins` — new subscribers from this source on this date. Sum across all sources for a given `date` equals (or closely approximates) `subscribers.joins` for the same date.

## Common joins

Outward forwarders of a post (which channels re-shared us):

```sql
SELECT c.link, c.name, c.subscribers
FROM public_shares s
JOIN public_channels c ON s.forwarder_link = c.link
WHERE s.post_id = ?;
```

Inward forward source of a post (which channel we forwarded from):

```sql
SELECT c.link, c.name, c.subscribers
FROM posts p
JOIN public_channels c ON p.forwarder_from_channel = c.link
WHERE p.id = ?;
```

Album items for a post:

```sql
SELECT attachment_id, link, media_type, photo_path
FROM post_attachments
WHERE post_id = ?
ORDER BY attachment_id;
```

Top posts by reactions (using the canonical latest-snapshot idiom):

```sql
WITH latest AS (
    SELECT MAX(id) AS id FROM post_metrics GROUP BY post_id
)
SELECT p.id, p.link, m.reactions, m.views,
       substr(p.text, 1, 80) AS snippet
FROM posts p
JOIN post_metrics m ON m.id IN (SELECT id FROM latest) AND m.post_id = p.id
ORDER BY m.reactions DESC
LIMIT 10;
```
