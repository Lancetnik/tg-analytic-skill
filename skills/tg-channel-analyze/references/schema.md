# Database schema (`data/<channel>.db`)

Read this before writing SQL through `tg_query.py`. One SQLite file per channel — leading `@` is stripped from the filename. No `channel` column anywhere; it's implicit in which DB you opened. Dates are ISO-8601 strings.

## `posts` (PK: `id`)

- `id` int — Telegram message id
- `link` text — `https://t.me/<channel>/<id>`
- `date` text — publish timestamp
- `text` text — post body
- `edit_date` text — non-null only if edited
- `reply_to_msg_id` int — if the post is a reply
- `tags` text — JSON array of hashtags (no `#`)
- `grouped_id` int — Telegram album id; non-null = multi-attachment post
- `forwarder_from_channel` text — if this post is itself a forward of another
  channel's post, the source channel's link (joins `public_channels.link`);
  null otherwise. Source channels are auto-added to `public_channels`.

## `post_attachments` (PK: `post_id`, `attachment_id`)

- `attachment_id` int — == `post_id` for single-media posts; differs for album members
- `link` text — `https://t.me/<channel>/<attachment_id>`
- `media_type` text — `photo`, `document`, or other Telethon class name
- `photo_path` text — local JPEG path (null when `--no-media`)

## `post_metrics` (PK: `id` autoincrement, **append-only time series**)

- `post_id` int — FK-style to `posts.id`
- `scrape_date` text — ISO timestamp of the scrape run
- `views`, `forwards`, `reactions`, `stars`, `comments_count`, `public_forwards_count` int
- Latest per post:

  ```sql
  SELECT ... FROM post_metrics
  WHERE (post_id, scrape_date) IN (
      SELECT post_id, MAX(scrape_date) FROM post_metrics GROUP BY post_id
  )
  ```

## `post_comments` (PK: `post_id`, `id`)

- `id` int — comment message id
- `date`, `text`, `author_id`, `author_name`, `author_username`

## `public_channels` (PK: `link`)

- `link` text — `https://t.me/<username>` or `https://t.me/c/<id>` for private
- `name`, `description`, `subscribers` — filled when `--channel-info` was on
- `last_seen` text — latest scrape that observed this channel

## `public_shares` (PK: `post_id`, `forwarder_link`, `msg_link`)

- `forwarder_link` text — joins to `public_channels(link)`
- `msg_link` text — the forwarded message in the outer channel
- `first_seen` text — scrape timestamp that first observed this share

## `subscribers` (PK: `date`)

- `date` text — YYYY-MM-DD (UTC)
- `total` int — subscribers at end of day
- `joins`, `leaves` int — daily counts (leaves stored as positive)

## `subscriber_sources` (PK: `date`, `source`)

- `source` text — Telegram label (`URL`, `Search`, `Groups`, `Channels`,
  `Other`, etc.)
- `count` int — joins from this source on this date

## Common joins

- Latest engagement per post: `posts p JOIN post_metrics m ON p.id = m.post_id`
  (combine with the latest-per-post predicate above)
- Outward forwarders of a post (who re-shared us):
  `public_shares s JOIN public_channels c ON s.forwarder_link = c.link WHERE s.post_id = ?`
- Inward forward source of a post (who we forwarded from):
  `posts p JOIN public_channels c ON p.forwarder_from_channel = c.link WHERE p.id = ?`
- Album items: `post_attachments WHERE post_id = ?`
