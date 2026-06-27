# tg-scraper / tg-analytic-skill

A Claude Code **skill** that analyzes a Telegram channel (the author's own
[@fastnewsdev](https://t.me/fastnewsdev)). Not an app — a bundled CLI the skill
drives. Distributed via the `skills` npm CLI (`npx skills@latest add ...`).

## Layout

- `skills/tg-analytic-skill/` — the skill itself (read-only when installed).
  - `SKILL.md` — instructions Claude follows. Update it whenever commands change.
  - `scripts/tg_scrape.py` — the Telegram-facing **read** CLI (Telethon).
  - `scripts/tg_publish.py` — the Telegram-facing **write** CLI: publish paths
    (`schedule`/`reschedule`/`edit`). Isolated from the read scripts on purpose
    (docs/adr/0003).
  - `scripts/tg_query.py` — read-only SQL CLI over the per-channel SQLite DB.
  - `scripts/utils/` — support **package** for the CLIs, imported as `utils.*`
    (e.g. `from utils._common import …`); resolves because `scripts/` is on
    `sys.path`. Modules import each other relatively (`from ._common import …`).
    - `_common.py` — shared paths, the `SCHEMA` constant (**source of truth**
      for the DB layout), and DB open helpers. Stdlib-only so `tg_query.py`
      keeps its empty-dependencies property.
    - `_tg.py` — Telethon session/credential plumbing (`_credentials`,
      `make_client`, `channel_session`, `_require_session`) shared by
      `tg_scrape.py` and `tg_publish.py`. Telethon-dependent, so kept out of
      stdlib-only `_common.py`.
    - `_render.py` — pure-presentation Markdown renderers (`summarize_*`);
      plain dicts in, stdout out — no Telethon or SQLite types.
    - `_md2entities.py` — `tg_publish.py` only: walks mistune's Markdown AST
      straight to Telethon `MessageEntity` objects (no HTML, no sulguk). Tables
      render as monospace `pre`; UTF-16 offset accounting lives here.
    - `_group.py` — discussion-group service/admin-log classification helpers
      used by `tg_scrape.py group`.
  - `references/schema.md` — restates `SCHEMA` for the SQL-writing agent; read
    before writing SQL.
  - `references/markup.md` — supported Markdown→Telegram markup for
    `tg_publish.py`; read before writing a post body.
  - `.env.example` — template for the 3 credentials.
- `tools/check_schema_doc.py` — **dev-only** drift guard, kept *outside* the
  skill so it isn't shipped to users; run after editing `SCHEMA` or
  `references/schema.md`.
- `.tg-analytic/` — **runtime state at the project root** (cwd), gitignored:
  `.env`, `session.session`, one `<channel>.db` per channel, `media/`. The
  scripts anchor this on `Path.cwd()`, so always run from the project root.

## Stack

- Python ≥3.10, run via `uv run` (PEP-723 inline deps in each script header).
  Shared helpers live in the `scripts/utils/` package; the CLIs import them as
  `from utils._x import …`, which resolves because the script's own directory
  (`scripts/`) is on `sys.path`.
- **mistune** — `tg_publish.py` only: parses the Markdown post body to an AST,
  which `_md2entities.py` walks straight to Telethon `MessageEntity` objects (no
  HTML hop, no sulguk). mistune (CommonMark-ish) over Python-Markdown on
  purpose: it keeps `#hashtag` lines literal instead of parsing them as `<h1>`,
  and lets a list interrupt a paragraph without a blank line. Pure-Python, zero
  transitive deps. Not needed by the read/query scripts. (RichText was a
  dead-end: it's Instant-View-only — messages carry only text + MessageEntity.)
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

## tg_publish.py commands

| Command | Does | Needs |
| --- | --- | --- |
| `schedule` | queue a Markdown post (body from `--file` or stdin) to publish at `--at`; Markdown→Telethon entities via `_md2entities`; prints confirmation, no DB write | **post rights** + session |
| `reschedule` | move scheduled post `--id` to a new `--at` (body unchanged); re-applies the 1h floor | **post rights** + session |
| `edit` | replace scheduled post `--id`'s body (from `--file` or stdin, time unchanged); **no** floor check | **post rights** + session |

`--id` is the `sched-msg` id from `tg_scrape.py scheduled`. `--at` is ISO-8601
**with offset** (naive rejected); the now+1h floor is a hardcoded `MIN_LEAD`
constant with no CLI/env override — the guard exists so the agent can't
schedule too soon (docs/adr/0003). `reschedule`/`edit` are `editMessage` with
`schedule_date`; Telethon returns `None` for scheduled edits, so the commands
report from known inputs, not the call result. The body (`schedule`/`edit`)
comes from `--file PATH` or stdin (`--file -`, or omit it); pipe a quoted
heredoc to publish a draft's clean body without writing a temp file (the CLI
strips no metainfo — pass only the body).

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
- `group_messages` is the **only** comment store (docs/adr/0002 superseded
  the separate `post_comments` table): `scrape`/`fetch` replace each post's
  thread (`thread_post_id` = post id), `group` upserts its scan window.
  Engagement queries always filter `is_thread_root = 0`. `group_events` PK
  is `(id, user_id)` — one add-user service message can carry several users.
