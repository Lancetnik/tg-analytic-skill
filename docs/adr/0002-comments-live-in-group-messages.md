# Comments live in group_messages; post_comments is dropped

Supersedes the storage half of
[ADR 0001](0001-self-contained-group-messages.md).

`scrape`/`fetch` persist each post's comment thread into `group_messages`
(`thread_post_id` = the post id, `is_thread_root` = 0) as a scoped
replace: `DELETE ... WHERE thread_post_id = ? AND is_thread_root = 0`,
then upsert. The `group` command's scan path is unchanged. The
`post_comments` table is dropped (`DROP TABLE IF EXISTS` on every DB
open, so old files self-heal); data is rescraped, not migrated.

This works because comment ids and group-message ids are one id space:
`iter_messages(channel, reply_to=post_id)` returns the linked discussion
group's messages. Every `post_comments` row was a `group_messages` row
minus the engagement columns.

ADR 0001 rejected "Unify" as routing comment *fetching* through the group
side — that coupling is still rejected; both fetch paths survive. What it
never considered was unifying only the *storage*: with one table there is
no "UNION over mismatched schemas" problem, and the SQL-writing agent
loses the error-prone counts-vs-engagement two-table rule.

## Considered Options

- **Keep both tables** (status quo per ADR 0001) — rejected: the split
  was the most misused query convention.
- **Compatibility VIEW named `post_comments`** — rejected: the channel
  owner can simply rescrape; a view keeps a dead convention alive.
- **Merge storage, keep both writers** — chosen.

## Consequences

- Per-post comment counts: `WHERE is_thread_root = 0 GROUP BY
  thread_post_id` (see "Thread stats per post" in references/schema.md).
- A scrape also freshens comment reactions — `group_messages` reflects
  whichever command wrote last.
- `group_messages` has no group-id column; if the channel ever switched
  discussion groups, ids could collide. Pre-existing quirk that now also
  covers comments — accepted.
- DBs touched only by read-only `tg_query.py` keep a stale
  `post_comments` until the next write-side command runs.
