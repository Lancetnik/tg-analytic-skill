---
name: tg-analytic-skill
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
- Always confirm the channel handle with the user before the first scrape — a typo silently creates a new empty DB at `.tg-analytic/<typo>.db`.

## Reporting back to the user

Every command prints a Markdown summary block to stdout. Use it as-is:

1. One-line headline (channel, time range, headline metric).
2. Paste the script's stdout summary.
3. If the user asked for depth the summary doesn't cover, follow with a
   `tg_query.py` result (Markdown table — already formatted for chat).

Don't paraphrase the summary; the script already pre-computes the
most-asked questions.

## First-run setup (do this before any scraping)

All runtime state — credentials, Telegram session, per-channel DBs, downloaded media — lives in `.tg-analytic/` at your **project root** (the cwd you launch the script from). The skill itself is read-only. Run all commands from the project root, not from inside the skill directory.

The `scrape`/`fetch`/`subscribers`/`views` commands need two things in `.tg-analytic/`: a `.env` with Telegram API credentials, and a `session.session` from a one-time interactive login. The `query` command needs neither.

If `.tg-analytic/.env` is missing:

1. Ask the user for their `TG_API_ID`, `TG_API_HASH`, and `TG_PHONE` (international format, e.g. `+15551234567`). Point them at https://my.telegram.org/apps to create credentials if they don't have them.
2. Create `.tg-analytic/` at the project root, then copy the skill's `.env.example` to `.tg-analytic/.env` and fill in the values.

If `.tg-analytic/session.session` is missing, the next scrape/fetch/subscribers/views command will exit with an explicit error. When that happens, **stop and tell the user to run**:

```
uv run skills/tg-analytic-skill/scripts/tg_scrape.py login
```

**in their own terminal** (not via you), from the project root — Telethon prompts on stdin for an SMS code and a 2FA password if enabled, which only works in an interactive TTY. Once it writes `.tg-analytic/session.session`, re-run the original command.

## CLIs

Two CLIs under `skills/tg-analytic-skill/scripts/`:

- **`tg_scrape.py`** - talks to Telegram. Commands: `scrape`, `fetch`,
  `subscribers`, `views`.
- **`tg_query.py`** - read-only SQL against the per-channel SQLite DB at
  `.tg-analytic/<channel>.db` (leading `@` stripped from filename).

Run from the **project root** with `uv run skills/tg-analytic-skill/scripts/<script>.py ...` — the scripts anchor `.tg-analytic/` on the current working directory. Always pass `--channel @name` explicitly. Every command prints a Markdown summary to stdout; lead with that when reporting to the user, then drop into `tg_query.py` for anything deeper.

`subscribers` and `views` require the account to be an **admin** of the
channel, and the channel must be eligible for Telegram stats (~500+ subs). If not, the command logs a clear error and exits 1.

## Pick the pattern matching the user's intent

All `scrape`/`fetch` runs persist to `.tg-analytic/<channel>.db` and **append** a
`post_metrics` row per post per run, so repeated runs build a time series.
Posts, comments, attachments, and forwarder shares are upserted/replaced.

### Choose the flag first — never default to `--limit`

`scrape` has four mutually exclusive selection modes. Pick exactly one based on what the user actually said. **Default to `--latest`, not `--limit`.**

| User said... | Flag | Why this one |
| --- | --- | --- |
| "latest 10", "newest 10", "last 10", "10 most recent" | `--latest 10` | The only flag that iterates **newest-first**. Use whenever the user counts posts from the present. |
| "posts from this week", "last 7 days", "since 2026-05-01", "after May 1" | `--offset-date DD-MM-YYYY` | Time-window framing. Compute the date locally; boundary is **exclusive** (strictly after). |
| "posts after #1234", "from post 1234 onward", "resume scrape", "incremental refresh" | `--offset-id 1234` | Cursor-based forward walk, **inclusive** of 1234. Standard incremental pattern: read `MAX(id)` from the DB, pass it in. |
| Specific known ids: "post 226", "refresh 103, 105, 108" | `fetch 103 105 108` (separate command) | One Telegram round-trip, no scan. Cheaper than `scrape --offset-id ... --limit 1`. |
| First-ever scrape, "full history", "all posts" | *(no flag)* | Walks oldest→newest from message 1. Slow; only run once per channel. |

`--limit N` is **not a selection flag** — it's a cap that bounds one of the above. **Used alone it walks oldest-first from message 1 and stops after N**, which on a populated channel re-scrapes ancient history instead of returning recent posts. Only use `--limit` to bound a forward page after an offset, e.g. `--offset-id 299 --limit 1` to grab a single specific post.

Worked examples of the three common requests:

```
# "scrape 10 latest posts"
uv run skills/tg-analytic-skill/scripts/tg_scrape.py scrape --channel @name --latest 10

# "scrape posts from the last week"   →   date 7 days ago, DD-MM-YYYY
uv run skills/tg-analytic-skill/scripts/tg_scrape.py scrape --channel @name --offset-date 21-05-2026

# "scrape posts after #1234"
uv run skills/tg-analytic-skill/scripts/tg_scrape.py scrape --channel @name --offset-id 1234
```

### 1. Initial channel scrape (full history)

```
uv run skills/tg-analytic-skill/scripts/tg_scrape.py scrape --channel @name
```

For a fast first look before committing to a full scrape of an unfamiliar
channel, use `--latest N` (newest-first) — never `--limit N`:

```
uv run skills/tg-analytic-skill/scripts/tg_scrape.py scrape --channel @name --latest 100 --no-media
```

### 2. Reindex specific posts (refresh metrics / comments / forwarders) — `fetch`

```
uv run skills/tg-analytic-skill/scripts/tg_scrape.py fetch 103 105 108 --channel @name
```

Appends a new `post_metrics` row per id; replaces comments/attachments/shares for those posts. Missing ids are logged and skipped. Album members auto-group by `grouped_id`.

Refresh only views/forwards/reactions (cheapest):

```
uv run skills/tg-analytic-skill/scripts/tg_scrape.py fetch 103 105 108 --channel @name \
    --no-comments --no-media --no-channel-info
```

To pick ids worth reindexing (e.g. recent bangers), pre-query the DB and pass the ids into `fetch`.

## Other commands

### `subscribers` - audience growth & churn

```
uv run skills/tg-analytic-skill/scripts/tg_scrape.py subscribers --channel @name
```

Prints date range, current total, net change, joins/leaves, daily averages,
best/worst day, and new subscribers broken down by source. Upserts into
`subscribers` (date|total|joins|leaves) and `subscriber_sources`
(date|source|count). Repeated runs accumulate history beyond Telegram's
retention window.

### `views` - best time to post

```
uv run skills/tg-analytic-skill/scripts/tg_scrape.py views --channel @name
```

Prints views per hour of day (0-23): peak hours, quietest hours, and the full
24-hour breakdown. Console output only.

Hours are in the **Telegram account's local timezone**, not UTC - that's what
the stats API returns and there's no offset to convert from. When reporting
peak hours to the user, say e.g. "20:00 local time (channel admin's tz)" so
they don't misread it as UTC.

### `query` - ad-hoc SQL

Read [references/schema.md](references/schema.md) before writing SQL with
`tg_query.py`. It documents every table, primary key, and the common joins
(latest metric per post, outward forwarders, inward citation, album items).

```
uv run skills/tg-analytic-skill/scripts/tg_query.py --channel @name \
  "SELECT p.id, p.link, m.views FROM posts p JOIN post_metrics m ON p.id = m.post_id ORDER BY m.views DESC LIMIT 10"
```

Read-only (SQLite `mode=ro`, writes rejected by the engine - safe for
LLM-generated SQL). Output is a Markdown table. `--limit N` caps rows (default
100, `0` = unlimited). `--no-truncate` to see full cell content (post body,
long comments). Use whenever the user asks for data not in the stdout summary.


## Validation

After running a command, sanity-check the result before reporting:

- After `scrape` / `fetch` — confirm rows landed:
  ```
  uv run skills/tg-analytic-skill/scripts/tg_query.py --channel @name \
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
| `Telegram session not found at .tg-analytic/session.session` | First run, or session deleted | Tell user to run `uv run skills/tg-analytic-skill/scripts/tg_scrape.py login` in their own terminal, from the project root. Do not try to run it yourself — it needs interactive stdin. |
| `failed to get stats ... you must be an admin of a channel that is large enough` | Account isn't admin, or channel < ~500 subs | Skill cannot do `subscribers`/`views` here. Fall back to `scrape` + `post_metrics` for engagement signals. |
| `no followers graph available` / `no top-hours graph available` | Stats exist but the requested graph is empty | Report to user; no retry helps. |
| New, empty `.tg-analytic/<handle>.db` appeared | Channel handle typo | Confirm the handle with the user; delete the empty DB before re-running. |

Telethon may also surface `FloodWaitError` mid-scrape on very large channels — the script logs and continues per item where possible. If a run aborts, re-run with `--offset-id <last-seen-id>` to resume forward rather than restart.

## Long-running history

`subscribers` and `views` periods are already the maximum Telegram offers. To build longer subscriber history, schedule `subscribers` periodically — upserts keep old rows.
