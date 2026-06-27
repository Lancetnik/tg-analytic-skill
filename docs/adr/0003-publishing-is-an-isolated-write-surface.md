# Publishing is an isolated write surface with a hardcoded lead time

The skill gains its first write capability — queuing a future channel post —
in a **separate `tg_publish.py` script**, not as a command in the read-only
`tg_scrape.py`. So "this code can publish" is auditable at the file level.
The shared Telethon session/credential helpers (`_credentials`, `make_client`,
`channel_session`) move into a new `_tg.py` imported by both scripts;
`_common.py` stays stdlib-only so `tg_query.py` keeps its zero-deps property.

Scheduling is **Telethon-only**: the Bot API cannot schedule messages
(`schedule_date` is an MTProto/user-client feature), so the existing
session-based approach is the only path.

The Markdown body becomes a post by walking **mistune's AST straight to
Telethon `MessageEntity` objects** (`_md2entities.py`) — no HTML round-trip,
no third-party HTML renderer. An earlier cut went Markdown → HTML →
`sulguk.transform_html` → Bot-API entity dicts → a TL shim; sulguk was dropped
because (a) Telegram messages are *only* plain text + `MessageEntity` offsets,
so the HTML hop is incidental; (b) sulguk raises on `<table>` — Telegram has no
table entity — whereas owning the walker lets us render tables as aligned
monospace `pre`; and (c) one fewer dependency, full control over UTF-16 offset
accounting. mistune (CommonMark-ish) is used rather than Python-Markdown
because a Telegram `#hashtag` line must stay literal text, but Python-Markdown
parses `#word` (no space) as an `<h1>` — silently destroying hashtags (found on
the first live test post).

Telegram's `RichText` type was considered and rejected: it belongs to
Instant-View `Page` objects, not messages — `messages.sendMessage` only accepts
`message` + `Vector<MessageEntity>`, so it cannot carry RichText. Styles that
exist only in RichText (subscript, superscript, highlight/`textMarked`) are
therefore unreachable in a message regardless of renderer.

Supported markup: `**bold**`, `*italic*`, `~~strike~~`, `^^underline^^`
(mistune `insert`), `||spoiler||` (a custom inline rule — mistune ships only
block `>!`), `` `code` ``, fenced code, `[text](url)`, `>` quote, bullet/ordered
lists, `#` headings (emulated as bold), and Markdown tables (monospace `pre`).
Hashtags and emoji pass through; emoji offsets are counted in UTF-16 units.

The **minimum lead time is a hardcoded `timedelta(hours=1)`** with no CLI flag
or env override, and offset-less (`naive`) `--at` values are rejected as
ambiguous. The guard exists to stop an *agent* from scheduling a post too
soon; a configurable floor the agent could pass would be the agent holding its
own leash. The human owner can still edit the constant in source.

## Commands on the queue

`tg_publish.py` exposes three commands, all keyed off the `sched-msg` id shown
by `tg_scrape.py scheduled`:

- `schedule --at` — queue a new post.
- `reschedule --id --at` — move an existing post to a new time, body
  unchanged. Re-applies the 1-hour floor (it sets a new publish time).
- `edit --id` — replace an existing post's body, time unchanged. **No**
  floor check: editing text never moves the publish time, so a typo-fix on an
  imminent post must not be blocked.

`schedule`/`edit` take the body from `--file PATH` or, when `--file` is `-` or
omitted, from **stdin**. stdin exists because drafts carry metainfo (header,
notes) the agent must strip before publishing: it produces the clean body and
pipes it via a quoted heredoc — no temp file, and backticks/`$`/quotes pass
verbatim (a raw `--text` argument would hit shell command-substitution on code
backticks and break on apostrophes). A TTY guard turns a bare interactive run
into a clear error rather than a hang. The CLI strips no metainfo itself —
drafts are too heterogeneous to parse reliably, so the agent owns body
selection.

Both `reschedule` and `edit` are `messages.editMessage` with `schedule_date`
set — that flag is what targets the *scheduled* message rather than a published
one with the same id, so `edit` first reads the existing schedule date and
re-sends it. Telethon's `edit_message` returns `None` for scheduled edits (it
can't map the `UpdateNewScheduledMessage` response back to a `Message`), so the
commands report from known inputs (the id is stable across an edit) rather than
the return value, and the benign "No random_id" warning from
`telethon.client.messageparse` is muted.

## Considered Options

- **New `schedule` command inside `tg_scrape.py`** — reuses infra with no
  duplication, but buries a publish action inside the read-only tool.
  Rejected: the write surface should be isolated and obvious.
- **Send via the Bot API** (like reagento/relator) — simpler entity handling
  (forward the dicts as-is), but the Bot API cannot schedule. Rejected.
- **Markdown → HTML → sulguk → TL shim** — the first implementation. Worked,
  but added a dep and an HTML hop, and crashed on tables. Replaced by the
  direct AST → `MessageEntity` walker.
- **Telegram `RichText`** — not usable for messages (Instant-View pages only).
- **Configurable / overridable lead time** — rejected: it would let the agent
  bypass the very guard it is meant to enforce.

## Consequences

- A scheduled post is not persisted: its id is a scheduled-message id distinct
  from the published-post id, and it carries no engagement (same rationale as
  the read-only `scheduled` command).
- `tg_publish.py` carries an extra dep surface (`mistune`) the read/query
  scripts don't need; acceptable since they are separate scripts.
- We own the Markdown→entity rendering in `_md2entities.py`: a new format means
  extending the AST walker, and UTF-16 offset accounting is our responsibility
  (verified by live render checks rather than a unit suite).
- Human approval for the write commands rides on Claude Code permissions:
  `tg_publish.py` is **not** in the `allow` list, so every run prompts by
  default — there is no in-script confirmation. An explicit `ask` rule (which
  overrides `allow`, so the prompt survives even a future broad allow-listing)
  was tried and then removed; if scheduling is ever allow-listed, re-add an
  `ask` rule for `edit`/`reschedule` to keep them gated. Any such guard is
  repo-local — it does **not** travel with the distributed skill, so a consumer
  who wants an approval gate must configure their own (or fall back to the
  `login`-style TTY pattern). The hardcoded 1-hour floor, by contrast, is in
  the script and travels with it.
