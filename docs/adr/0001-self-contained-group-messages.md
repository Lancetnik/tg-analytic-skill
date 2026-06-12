# Group analytics is self-contained; post_comments stays the channel-scrape view

The `group` command stores **every** non-service message of a discussion group
in `group_messages` — including comments that `scrape` already captures in
`post_comments`. The duplication is deliberate, not a bug: `post_comments` has
no `reply_to_msg_id`, no reactions, and no thread-root linkage, so it cannot
answer thread-structure or per-message-engagement questions; the group scraper
needs full-fidelity rows regardless, and excluding comments would only force
UNION queries over two differently-shaped tables.

## Considered Options

- **Complement-only** (store only what `post_comments` lacks) — rejected:
  every cross-group engagement query becomes a UNION over mismatched schemas.
- **Unify** (route comment scraping through the group side) — rejected:
  couples a new feature to the proven `scrape` path for no user-visible gain.

## Consequences

- Comment counts per post → query `post_comments`; thread structure,
  reactions, per-user engagement → query `group_messages`. The rule lives in
  `references/schema.md`.
- Auto-forwarded thread roots are rows too (`is_thread_root = 1`) and carry
  the channel post's reactions — engagement aggregates must filter them out.
