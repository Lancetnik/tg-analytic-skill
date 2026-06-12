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

<!-- rtk-instructions v2 -->
# RTK (Rust Token Killer) - Token-Optimized Commands

## Golden Rule

**Always prefix commands with `rtk`**. If RTK has a dedicated filter, it uses it. If not, it passes through unchanged. This means RTK is always safe to use.

**Important**: Even in command chains with `&&`, use `rtk`:
```bash
# ❌ Wrong
git add . && git commit -m "msg" && git push

# ✅ Correct
rtk git add . && rtk git commit -m "msg" && rtk git push
```

## RTK Commands by Workflow

### Build & Compile (80-90% savings)
```bash
rtk cargo build         # Cargo build output
rtk cargo check         # Cargo check output
rtk cargo clippy        # Clippy warnings grouped by file (80%)
rtk tsc                 # TypeScript errors grouped by file/code (83%)
rtk lint                # ESLint/Biome violations grouped (84%)
rtk prettier --check    # Files needing format only (70%)
rtk next build          # Next.js build with route metrics (87%)
```

### Test (60-99% savings)
```bash
rtk cargo test          # Cargo test failures only (90%)
rtk go test             # Go test failures only (90%)
rtk jest                # Jest failures only (99.5%)
rtk vitest              # Vitest failures only (99.5%)
rtk playwright test     # Playwright failures only (94%)
rtk pytest              # Python test failures only (90%)
rtk rake test           # Ruby test failures only (90%)
rtk rspec               # RSpec test failures only (60%)
rtk test <cmd>          # Generic test wrapper - failures only
```

### Git (59-80% savings)
```bash
rtk git status          # Compact status
rtk git log             # Compact log (works with all git flags)
rtk git diff            # Compact diff (80%)
rtk git show            # Compact show (80%)
rtk git add             # Ultra-compact confirmations (59%)
rtk git commit          # Ultra-compact confirmations (59%)
rtk git push            # Ultra-compact confirmations
rtk git pull            # Ultra-compact confirmations
rtk git branch          # Compact branch list
rtk git fetch           # Compact fetch
rtk git stash           # Compact stash
rtk git worktree        # Compact worktree
```

Note: Git passthrough works for ALL subcommands, even those not explicitly listed.

### GitHub (26-87% savings)
```bash
rtk gh pr view <num>    # Compact PR view (87%)
rtk gh pr checks        # Compact PR checks (79%)
rtk gh run list         # Compact workflow runs (82%)
rtk gh issue list       # Compact issue list (80%)
rtk gh api              # Compact API responses (26%)
```

### JavaScript/TypeScript Tooling (70-90% savings)
```bash
rtk pnpm list           # Compact dependency tree (70%)
rtk pnpm outdated       # Compact outdated packages (80%)
rtk pnpm install        # Compact install output (90%)
rtk npm run <script>    # Compact npm script output
rtk npx <cmd>           # Compact npx command output
rtk prisma              # Prisma without ASCII art (88%)
```

### Files & Search (60-75% savings)
```bash
rtk ls <path>           # Tree format, compact (65%)
rtk read <file>         # Code reading with filtering (60%)
rtk grep <pattern>      # Search grouped by file (75%). Format flags (-c, -l, -L, -o, -Z) run raw.
rtk find <pattern>      # Find grouped by directory (70%)
```

### Analysis & Debug (70-90% savings)
```bash
rtk err <cmd>           # Filter errors only from any command
rtk log <file>          # Deduplicated logs with counts
rtk json <file>         # JSON structure without values
rtk deps                # Dependency overview
rtk env                 # Environment variables compact
rtk summary <cmd>       # Smart summary of command output
rtk diff                # Ultra-compact diffs
```

### Infrastructure (85% savings)
```bash
rtk docker ps           # Compact container list
rtk docker images       # Compact image list
rtk docker logs <c>     # Deduplicated logs
rtk kubectl get         # Compact resource list
rtk kubectl logs        # Deduplicated pod logs
```

### Network (65-70% savings)
```bash
rtk curl <url>          # Compact HTTP responses (70%)
rtk wget <url>          # Compact download output (65%)
```

### Meta Commands
```bash
rtk gain                # View token savings statistics
rtk gain --history      # View command history with savings
rtk discover            # Analyze Claude Code sessions for missed RTK usage
rtk proxy <cmd>         # Run command without filtering (for debugging)
rtk init                # Add RTK instructions to CLAUDE.md
rtk init --global       # Add RTK to ~/.claude/CLAUDE.md
```

## Token Savings Overview

| Category | Commands | Typical Savings |
|----------|----------|-----------------|
| Tests | vitest, playwright, cargo test | 90-99% |
| Build | next, tsc, lint, prettier | 70-87% |
| Git | status, log, diff, add, commit | 59-80% |
| GitHub | gh pr, gh run, gh issue | 26-87% |
| Package Managers | pnpm, npm, npx | 70-90% |
| Files | ls, read, grep, find | 60-75% |
| Infrastructure | docker, kubectl | 85% |
| Network | curl, wget | 65-70% |

Overall average: **60-90% token reduction** on common development operations.
<!-- /rtk-instructions -->