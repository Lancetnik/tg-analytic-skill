# Database schema (`.tg-analytic/<channel>.db`)

Read this before writing SQL through `tg_query.py`. One SQLite file per channel — leading `@` is stripped from the filename. **There is no `channel` column anywhere** — the channel is implicit in which DB you opened. Don't `WHERE channel = ...`.

The literal `CREATE TABLE` statements below restate the `SCHEMA` constant in `scripts/_common.py` (the source of truth, kept in sync by `scripts/check_schema_doc.py`) — every column the agent can SELECT, JOIN, or filter on appears there. Notes underneath each table cover only what the DDL can't convey (storage format, semantics, gotchas).

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
    author           TEXT GENERATED ALWAYS AS (
        COALESCE(user_username, user_name, CAST(user_id AS TEXT))
    ) VIRTUAL,
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

CREATE TABLE group_messages (
    id               INTEGER PRIMARY KEY,
    date             TEXT,
    text             TEXT,
    user_id          INTEGER,
    user_name        TEXT,
    user_username    TEXT,
    author           TEXT GENERATED ALWAYS AS (
        COALESCE(user_username, user_name, CAST(user_id AS TEXT))
    ) VIRTUAL,
    reply_to_msg_id  INTEGER,
    thread_post_id   INTEGER,
    is_thread_root   INTEGER NOT NULL DEFAULT 0,
    reactions        INTEGER,
    media_type       TEXT
);
CREATE INDEX idx_group_messages_thread ON group_messages(thread_post_id);

CREATE TABLE group_events (
    id             INTEGER NOT NULL,
    date           TEXT,
    kind           TEXT,
    via            TEXT,
    user_id        INTEGER,
    user_name      TEXT,
    user_username  TEXT,
    author         TEXT GENERATED ALWAYS AS (
        COALESCE(user_username, user_name, CAST(user_id AS TEXT))
    ) VIRTUAL,
    PRIMARY KEY (id, user_id)
);

CREATE TABLE group_metrics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_date  TEXT NOT NULL,
    group_link   TEXT,
    group_title  TEXT,
    members      INTEGER
);
```

Date/time columns are ISO-8601 strings throughout (`posts.date`, `posts.edit_date`, `post_comments.date`, `public_channels.last_seen`, `public_shares.first_seen`, `post_metrics.scrape_date`, `group_messages.date`, `group_events.date`, `group_metrics.scrape_date`). Use SQLite's `date()`, `datetime()`, `strftime()` directly — no conversion needed.

## Repost direction cheat-sheet

Two opposite directions live in different tables — don't mix them up:

| Direction | Question it answers | Where |
| --- | --- | --- |
| **YOU reposted someone** | "which of my posts are forwards of other channels' content?" | `posts.forwarder_from_channel` (non-NULL = that post is not original) |
| **Someone reposted YOU** | "which channels re-shared my posts?" (your reach) | `public_shares` (one row per re-share), count in `post_metrics.public_forwards_count` |

`post_metrics.forwards` is a third thing: Telegram's raw forward counter on your post — forwards by anyone, anywhere (including private chats/users), superset of `public_forwards_count`.

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
- `forwarder_from_channel` — direction: **YOU reposted them.** If this post is a forward of another channel's post (i.e. not your original content), the source channel link (joins `public_channels.link`); NULL otherwise. The source channel is auto-inserted into `public_channels`. For the opposite direction (others reposting you) see `public_shares`.

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
    author           TEXT GENERATED ALWAYS AS (
        COALESCE(user_username, user_name, CAST(user_id AS TEXT))
    ) VIRTUAL,
    PRIMARY KEY (post_id, id)
);
```

- `post_id` — FK to `posts.id`.
- `id` — comment message id in the linked discussion group. Unique only within `post_id`.
- `user_id` — Telegram id of the commenter. When a comment was posted *as a channel* (Telegram's "send as" feature), this is the channel's id and `user_name`/`user_username` carry the channel's title/username.
- `user_username` — without the leading `@`; NULL if the commenter has no public username.
- `user_name` — display name; may be NULL or anonymized.
- `author` — derived convenience identity: best available human-readable name for the commenter (`user_username`, else `user_name`, else `user_id` as text). Generated VIRTUAL column — computed on read, never stored or written. Use it for `GROUP BY author` / `COUNT(DISTINCT author)`. Caveat: two username-less commenters sharing a display name collapse into one `author` value; use `user_id` when exactness matters.

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

## `public_shares` — who re-shared YOUR posts

Direction: **someone reposted YOU.** For the opposite direction (your channel reposting others) see `posts.forwarder_from_channel`.

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

## `group_messages` — the discussion group, self-contained

Written by the `group` command. **Deliberately overlaps `post_comments`**
(see ADR-0001): comment counts per post → `post_comments`; thread
structure, reactions, per-user engagement → here. Don't count comments
from both.

```sql
CREATE TABLE group_messages (
    id               INTEGER PRIMARY KEY,
    date             TEXT,
    text             TEXT,
    user_id          INTEGER,
    user_name        TEXT,
    user_username    TEXT,
    author           TEXT GENERATED ALWAYS AS (
        COALESCE(user_username, user_name, CAST(user_id AS TEXT))
    ) VIRTUAL,
    reply_to_msg_id  INTEGER,
    thread_post_id   INTEGER,
    is_thread_root   INTEGER NOT NULL DEFAULT 0,
    reactions        INTEGER,
    media_type       TEXT
);
```

- `id` — group-side message id. Distinct id space from `posts.id`.
- `thread_post_id` — **channel** post id of the thread this message
  belongs to (joins `posts.id` directly). NULL = top-level chatter.
  Always NULL for a standalone group's DB.
- `reply_to_msg_id` — raw group-side parent id (reply chains *within* a
  thread). Don't join it to `posts`.
- `is_thread_root` — 1 = the auto-forwarded channel post heading a
  thread. **Engagement aggregates must filter `is_thread_root = 0`** —
  roots carry the channel post's reactions and would double-count.
- `reactions` — reaction count at last scan (upserted in place, not a
  time series; paid stars folded in).
- `author` — same generated convenience identity as `post_comments`.

## `group_events` — joins & leaves

```sql
CREATE TABLE group_events (
    id             INTEGER NOT NULL,
    date           TEXT,
    kind           TEXT,
    via            TEXT,
    user_id        INTEGER,
    user_name      TEXT,
    user_username  TEXT,
    author         TEXT GENERATED ALWAYS AS (
        COALESCE(user_username, user_name, CAST(user_id AS TEXT))
    ) VIRTUAL,
    PRIMARY KEY (id, user_id)
);
```

- `id` — service-message id. PK is `(id, user_id)`: one add-user service
  message can add several users.
- `kind` — `join` | `leave`.
- `via` — joins: `link` (invite/CTA link) | `request` (approved join
  request) | `added` (added by a member, **or** the group's Join button —
  Telegram encodes a self-join as the user adding themselves). Leaves:
  `self` | `removed`.
- Completeness caveat: Telegram suppresses join/leave service messages in
  very large groups — cross-check against `group_metrics.members`.

## `group_metrics` — append-only snapshots

```sql
CREATE TABLE group_metrics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_date  TEXT NOT NULL,
    group_link   TEXT,
    group_title  TEXT,
    members      INTEGER
);
```

- Same idiom as `post_metrics`: one row per `group` run, **`MAX(id)` =
  latest snapshot**, not `MAX(scrape_date)`.
- `group_link`/`group_title` double as the identity record of which
  group this DB's group_* rows came from.
- `members` — participants_count at scan time; drift vs cumulative
  `joins - leaves` reveals event-log gaps.

## Common joins

Who re-shared YOUR post (other channels that forwarded your content):

```sql
SELECT c.link, c.name, c.subscribers
FROM public_shares s
JOIN public_channels c ON s.forwarder_link = c.link
WHERE s.post_id = ?;
```

Whose post YOU reposted (the source channel your post was forwarded from):

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

Joins attributable to a post's CTA (window: post publish + N days):

```sql
SELECT COUNT(*) AS joins
FROM group_events e, posts p
WHERE p.id = :post_id
  AND e.kind = 'join'
  AND e.date >= p.date
  AND datetime(e.date) < datetime(p.date, '+7 days');
```

(`datetime(e.date)` normalizes the stored `T`-separated ISO string to
SQLite's space-separated form so the boundary-day comparison is exact.)

Thread stats per post (engagement excludes roots — always):

```sql
SELECT gm.thread_post_id AS post_id, COUNT(*) AS replies,
       COUNT(DISTINCT gm.author) AS commenters
FROM group_messages gm
WHERE gm.is_thread_root = 0 AND gm.thread_post_id IS NOT NULL
GROUP BY gm.thread_post_id
ORDER BY replies DESC;
```
