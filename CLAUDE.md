# tg-scraper / tg-analytic-skill

A Claude Code **skill** that analyzes a Telegram channel (the author's own
[@fastnewsdev](https://t.me/fastnewsdev)). Not an app — a bundled CLI the skill
drives. Distributed via the `skills` npm CLI (`npx skills@latest add ...`).

## Layout

- `skills/tg-analytic-skill/` — the skill itself (read-only when installed).
  - `SKILL.md` — instructions Claude follows. Update it whenever commands change.
  - `scripts/tg_scrape.py` — the Telegram-facing CLI (Telethon).
  - `scripts/tg_query.py` — read-only SQL CLI over the per-channel SQLite DB.
  - `scripts/_common.py` — shared paths, the `SCHEMA` constant (**source of
    truth** for the DB layout), and DB open helpers. Stdlib-only so
    `tg_query.py` keeps its empty-dependencies property.
  - `scripts/_render.py` — pure-presentation Markdown renderers (`summarize_*`);
    plain dicts in, stdout out — no Telethon or SQLite types.
  - `scripts/check_schema_doc.py` — drift guard; run after editing `SCHEMA` or
    `references/schema.md`.
  - `references/schema.md` — restates `SCHEMA` for the SQL-writing agent; read
    before writing SQL.
  - `.env.example` — template for the 3 credentials.
- `.tg-analytic/` — **runtime state at the project root** (cwd), gitignored:
  `.env`, `session.session`, one `<channel>.db` per channel, `media/`. The
  scripts anchor this on `Path.cwd()`, so always run from the project root.

## Stack

- Python ≥3.10, run via `uv run` (PEP-723 inline deps in each script header).
  Same-directory imports (`_common`, `_render`) resolve because the script's
  own directory is on `sys.path`.
- **Telethon** (`>=1.36,<2`) — Telegram client API (not the bot API). Auth = a
  `session.session` file from a one-time interactive `login` (needs a TTY for
  the SMS code, so it can't run via the Bash tool — tell the user to run it).
- **typer** for the CLI, **SQLite** for storage (one DB file per channel,
  leading `@` stripped from the filename). `tg_query.py` opens `?mode=ro`.

## tg_scrape.py commands

| Command | Does | Needs |
| --- | --- | --- |
| `login` | one-time interactive auth → writes session | TTY (user runs it) |
| `scrape` | posts + comments + media + forwarders → DB; appends a `post_metrics` row per run | session |
| `fetch <ids>` | refresh specific post ids (one round-trip, no scan) | session |
| `group` | discussion-group messages + threads + join/leave events → DB; appends a `group_metrics` row per run | membership in the group (`--channel @chan` for the linked group, or `--group @grp` standalone) |
| `subscribers` | growth/churn by source from stats API | **admin** + ~500+ subs |
| `views` | views per hour of day | **admin** + stats-eligible |
| `scheduled` | list not-yet-published posts (console-only, no DB) | **post rights** |

Scrape selection flags are mutually exclusive; default to `--latest N`
(newest-first), never bare `--limit N` (walks oldest-first from msg 1).

## Key architecture facts (non-obvious)

- `post_metrics` is **append-only** — use `MAX(id)` for "latest snapshot", not
  `MAX(scrape_date)`. See the canonical CTE in `references/schema.md`.
- Telethon TL types are dynamically generated, so Pyright flags `.sender`,
  `.chats`, `.full_chat`, `.forwards` etc. as unknown attributes throughout —
  these warnings are expected noise, not real errors.
- Every command prints a Markdown summary to stdout designed to be pasted to the
  user as-is. When adding a command, follow that convention (`summarize_*`).
- `group_messages` deliberately duplicates comments that `post_comments`
  also holds (see docs/adr/0001): thread structure / reactions / engagement →
  query `group_messages` (always filter `is_thread_root = 0`); per-post
  comment counts → `post_comments`. `group_events` PK is `(id, user_id)` —
  one add-user service message can carry several users.
