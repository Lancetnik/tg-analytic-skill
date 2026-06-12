# Discussion-Group Analytics (`group` command) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `group` subcommand to `tg_scrape.py` that scrapes a Telegram discussion group (or standalone group) into `group_messages` / `group_events` / `group_metrics`, and prints a Markdown summary with hour-of-day activity, join/leave breakdown, and per-thread stats.

**Architecture:** A new stdlib-only `_group.py` module holds the pure logic (service-message classification, thread-root linkage) using duck-typed attribute access so tests run without Telethon. `tg_scrape.py` gains the Telegram-facing scan + persistence; `_render.py` gains `summarize_group` (pure dicts in, stdout out). Three new tables in `SCHEMA` (`_common.py`), restated in `references/schema.md` and guarded by `check_schema_doc.py`.

**Tech Stack:** Python ≥3.10, Telethon, typer, SQLite, uv (PEP-723), pytest (test-time only, via `uv run --with pytest`).

**Spec:** the decision table in the grilling session, [CONTEXT.md](../../../CONTEXT.md), [ADR-0001](../../adr/0001-self-contained-group-messages.md). Key locked decisions:

- `group --channel @x` (resolve linked group via `linked_chat_id`, write to the channel's DB) XOR `group --group @y` (standalone, own DB, `thread_post_id` NULL everywhere, threads section omitted).
- `group_messages` is self-contained (comments duplicated by design; roots flagged `is_thread_root=1`; engagement queries exclude roots).
- `thread_post_id` = **channel** post id (resolved through the auto-forward map), `reply_to_msg_id` = raw group-side parent.
- Events from service messages only; `kind` ∈ join|leave, `via` ∈ link|request|added / self|removed.
- Selection flags mirror `scrape` exactly; upserts; no media download.
- Summary: Overview · joins/leaves by `via` + by day · hour-of-day table (0–23, local tz labeled: joins/messages/uniq authors) · all threads in window (replies, uniq authors, first-reply delta, post snippet) · engagement (top contributors, most-reacted message).

**One deliberate deviation from the grilled schema** (surface in the commit message): `group_events` PK is `(id, user_id)`, not `id` — one `MessageActionChatAddUser` service message can add **several** users, producing several events with the same service-msg id.

---

### Task 1: Test infrastructure

**Files:**
- Create: `tests/conftest.py`

- [ ] **Step 1: Create conftest that puts the scripts dir on sys.path**

```python
# tests/conftest.py
"""Tests import the skill's script modules directly. Only the stdlib-only
modules (_group, _render, _common) are imported by tests — no Telethon
needed, so plain `uv run --with pytest` suffices."""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "tg-analytic-skill" / "scripts"
sys.path.insert(0, str(SCRIPTS))
```

- [ ] **Step 2: Verify pytest collects (no tests yet — expect "no tests ran")**

Run: `cd /Users/nikitapastukhov/Desktop/work/tg-scraper && uv run --with pytest python -m pytest tests/ -q`
Expected: `no tests ran` (exit 5 is fine at this step)

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: add pytest scaffolding for skill script modules"
```

---

### Task 2: `_group.py` — service-message classification (TDD)

**Files:**
- Create: `skills/tg-analytic-skill/scripts/_group.py`
- Test: `tests/test_group_events.py`

`_group.py` must stay **stdlib-only** and dispatch on `type(action).__name__` (duck typing) so tests fabricate plain stand-in objects and the module never imports Telethon.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_group_events.py
from types import SimpleNamespace as NS
from datetime import datetime, timezone

from _group import classify_service_message


def _mk(name, msg_id=10, sender_id=111, date=None, **action_attrs):
    """Fabricate a Telethon-shaped service message. type(action).__name__
    is what _group dispatches on, so build a throwaway class per name."""
    action = type(name, (), {})()
    for k, v in action_attrs.items():
        setattr(action, k, v)
    return NS(
        id=msg_id,
        date=date or datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc),
        from_id=NS(user_id=sender_id),
        action=action,
    )


def test_joined_by_link():
    [e] = classify_service_message(_mk("MessageActionChatJoinedByLink"))
    assert (e.kind, e.via, e.user_id) == ("join", "link", 111)
    assert e.id == 10
    assert e.date == "2026-06-01T12:30:00+00:00"


def test_joined_by_request():
    [e] = classify_service_message(_mk("MessageActionChatJoinedByRequest"))
    assert (e.kind, e.via) == ("join", "request")


def test_add_user_fans_out_one_event_per_user():
    events = classify_service_message(
        _mk("MessageActionChatAddUser", users=[222, 333])
    )
    assert [(e.kind, e.via, e.user_id) for e in events] == [
        ("join", "added", 222),
        ("join", "added", 333),
    ]


def test_self_join_button_is_added():
    # Join-button joins arrive as the user adding themselves.
    [e] = classify_service_message(
        _mk("MessageActionChatAddUser", sender_id=222, users=[222])
    )
    assert (e.kind, e.via, e.user_id) == ("join", "added", 222)


def test_self_leave():
    [e] = classify_service_message(
        _mk("MessageActionChatDeleteUser", sender_id=222, user_id=222)
    )
    assert (e.kind, e.via, e.user_id) == ("leave", "self", 222)


def test_kick_is_removed():
    [e] = classify_service_message(
        _mk("MessageActionChatDeleteUser", sender_id=111, user_id=222)
    )
    assert (e.kind, e.via, e.user_id) == ("leave", "removed", 222)


def test_unrelated_service_message_yields_nothing():
    assert classify_service_message(_mk("MessageActionPinMessage")) == []


def test_ordinary_message_yields_nothing():
    assert classify_service_message(NS(id=1, date=None, from_id=None, action=None)) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest python -m pytest tests/test_group_events.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named '_group'`

- [ ] **Step 3: Implement `_group.py` (classification half)**

```python
# skills/tg-analytic-skill/scripts/_group.py
"""Discussion-group scan logic: service-message classification and thread
linkage. Stdlib-only and duck-typed over Telethon objects — dispatch is on
`type(action).__name__`, attribute access via getattr — so tg_query.py's
empty-deps sibling property holds and tests fabricate plain stand-ins.
"""

from dataclasses import dataclass


@dataclass
class GroupEvent:
    """One membership change. kind: 'join'|'leave'; via: join → 'link'|
    'request'|'added', leave → 'self'|'removed'."""
    id: int
    date: str | None
    kind: str
    via: str
    user_id: int | None


def _sender_user_id(msg) -> int | None:
    return getattr(getattr(msg, "from_id", None), "user_id", None)


def _iso(msg) -> str | None:
    date = getattr(msg, "date", None)
    return date.isoformat() if date else None


def classify_service_message(msg) -> list[GroupEvent]:
    """Map one service message to its membership events.

    One MessageActionChatAddUser can add several users → several events
    (why group_events' PK is (id, user_id)). 'added' covers both "added by
    a member" and the Join button — Telegram encodes a self-join as the
    user adding themselves. Non-membership service messages (pins, title
    changes, ...) yield []."""
    action = getattr(msg, "action", None)
    if action is None:
        return []
    name = type(action).__name__
    date = _iso(msg)
    sender = _sender_user_id(msg)
    if name == "MessageActionChatJoinedByLink":
        return [GroupEvent(msg.id, date, "join", "link", sender)]
    if name == "MessageActionChatJoinedByRequest":
        return [GroupEvent(msg.id, date, "join", "request", sender)]
    if name == "MessageActionChatAddUser":
        users = getattr(action, "users", None) or []
        return [GroupEvent(msg.id, date, "join", "added", uid) for uid in users]
    if name == "MessageActionChatDeleteUser":
        uid = getattr(action, "user_id", None)
        via = "self" if uid == sender else "removed"
        return [GroupEvent(msg.id, date, "leave", via, uid)]
    return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --with pytest python -m pytest tests/test_group_events.py -q`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_group_events.py skills/tg-analytic-skill/scripts/_group.py
git commit -m "feat(group): classify membership service messages into join/leave events"
```

---

### Task 3: `_group.py` — thread linkage (TDD)

**Files:**
- Modify: `skills/tg-analytic-skill/scripts/_group.py`
- Test: `tests/test_group_threads.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_group_threads.py
from types import SimpleNamespace as NS

from _group import auto_forward_post_id, thread_post_id_for, unresolved_root_refs

CHANNEL_ID = 9000


def _root(group_id=500, channel_post=42, channel_id=CHANNEL_ID):
    return NS(
        id=group_id,
        fwd_from=NS(channel_post=channel_post, from_id=NS(channel_id=channel_id)),
        reply_to=None,
    )


def _reply(msg_id, to=None, top=None):
    return NS(
        id=msg_id,
        fwd_from=None,
        reply_to=NS(reply_to_msg_id=to, reply_to_top_id=top),
    )


def test_auto_forward_resolves_channel_post_id():
    assert auto_forward_post_id(_root(), CHANNEL_ID) == 42


def test_manual_forward_from_other_channel_is_not_a_root():
    assert auto_forward_post_id(_root(channel_id=1234), CHANNEL_ID) is None


def test_standalone_group_has_no_roots():
    # channel_id None = standalone group: nothing is a thread root.
    assert auto_forward_post_id(_root(), None) is None


def test_non_forward_is_not_a_root():
    assert auto_forward_post_id(NS(id=1, fwd_from=None), CHANNEL_ID) is None


def test_direct_comment_resolves_via_reply_to_msg_id():
    root_map = {500: 42}
    assert thread_post_id_for(_reply(501, to=500), root_map) == 42


def test_nested_reply_resolves_via_reply_to_top_id():
    root_map = {500: 42}
    assert thread_post_id_for(_reply(502, to=501, top=500), root_map) == 42


def test_reply_to_top_level_chatter_is_not_in_a_thread():
    assert thread_post_id_for(_reply(503, to=400), {500: 42}) is None


def test_non_reply_is_top_level():
    assert thread_post_id_for(NS(id=504, reply_to=None), {500: 42}) is None


def test_unresolved_root_refs_collects_missing_thread_heads():
    msgs = [_reply(501, to=500), _reply(502, to=501, top=499), NS(id=503, reply_to=None)]
    assert unresolved_root_refs(msgs, {500: 42}) == {499}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest python -m pytest tests/test_group_threads.py -q`
Expected: FAIL — `ImportError: cannot import name 'auto_forward_post_id'`

- [ ] **Step 3: Append the linkage functions to `_group.py`**

```python
def auto_forward_post_id(msg, channel_id: int | None) -> int | None:
    """Channel post id if `msg` is the auto-forward of a channel post (a
    thread root); None otherwise.

    `channel_id` is the linked channel's id — it guards against manual
    forwards of unrelated channels' posts being mistaken for roots. None
    (standalone group) means no message is ever a root."""
    if channel_id is None:
        return None
    fwd = getattr(msg, "fwd_from", None)
    if fwd is None:
        return None
    post_id = getattr(fwd, "channel_post", None)
    if post_id is None:
        return None
    if getattr(getattr(fwd, "from_id", None), "channel_id", None) != channel_id:
        return None
    return post_id


def _thread_head(msg) -> int | None:
    """Group-side id of the thread head this message replies under.

    Nested replies carry the head in reply_to_top_id; direct comments on
    the root carry it in reply_to_msg_id (top_id absent)."""
    reply = getattr(msg, "reply_to", None)
    if reply is None:
        return None
    return (
        getattr(reply, "reply_to_top_id", None)
        or getattr(reply, "reply_to_msg_id", None)
    )


def thread_post_id_for(msg, root_map: dict[int, int]) -> int | None:
    """Which channel post's thread `msg` belongs to (None = top-level
    chatter). `root_map` maps group-side root id → channel post id."""
    head = _thread_head(msg)
    return root_map.get(head) if head is not None else None


def unresolved_root_refs(msgs, root_map: dict[int, int]) -> set[int]:
    """Thread heads referenced by `msgs` but absent from `root_map` —
    roots that fell outside the scanned window and need a targeted fetch.
    May include heads that turn out to be plain chatter (replies to a
    top-level message); the caller fetches and re-checks."""
    return {
        head
        for m in msgs
        if (head := _thread_head(m)) is not None and head not in root_map
    }
```

- [ ] **Step 4: Run the full test suite**

Run: `uv run --with pytest python -m pytest tests/ -q`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_group_threads.py skills/tg-analytic-skill/scripts/_group.py
git commit -m "feat(group): resolve thread membership through auto-forward root map"
```

---

### Task 4: SCHEMA — three new tables

**Files:**
- Modify: `skills/tg-analytic-skill/scripts/_common.py` (append to `SCHEMA`, before the closing `"""`)
- Test: `tests/test_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schema.py
import sqlite3

from _common import SCHEMA


def test_group_tables_exist_and_author_is_generated():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)

    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"group_messages", "group_events", "group_metrics"} <= tables

    # group_events PK is (id, user_id): one AddUser service msg, many users.
    conn.execute(
        "INSERT INTO group_events (id, date, kind, via, user_id) "
        "VALUES (10, '2026-06-01', 'join', 'added', 222)"
    )
    conn.execute(
        "INSERT INTO group_events (id, date, kind, via, user_id) "
        "VALUES (10, '2026-06-01', 'join', 'added', 333)"
    )

    # author generated column mirrors post_comments' COALESCE chain.
    conn.execute(
        "INSERT INTO group_messages (id, date, text, user_id, user_name) "
        "VALUES (1, '2026-06-01', 'hi', 5, 'Ann')"
    )
    [(author,)] = conn.execute("SELECT author FROM group_messages")
    assert author == "Ann"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest python -m pytest tests/test_schema.py -q`
Expected: FAIL — assertion on missing tables

- [ ] **Step 3: Append to `SCHEMA` in `_common.py`** (insert before the closing `"""` of the constant)

```sql
CREATE TABLE IF NOT EXISTS group_messages (
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

CREATE INDEX IF NOT EXISTS idx_group_messages_thread
    ON group_messages(thread_post_id);

CREATE TABLE IF NOT EXISTS group_events (
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

CREATE TABLE IF NOT EXISTS group_metrics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_date  TEXT NOT NULL,
    group_link   TEXT,
    group_title  TEXT,
    members      INTEGER
);
```

(No `_add_missing_columns` change needed — these are whole new tables; `CREATE TABLE IF NOT EXISTS` in `open_db`'s `executescript` creates them in existing DBs.)

- [ ] **Step 4: Run tests**

Run: `uv run --with pytest python -m pytest tests/ -q`
Expected: 18 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_schema.py skills/tg-analytic-skill/scripts/_common.py
git commit -m "feat(group): add group_messages/group_events/group_metrics tables

group_events PK is (id, user_id), not bare id: one ChatAddUser service
message can add several users."
```

---

### Task 5: `references/schema.md` — document the tables + cheat-sheet rules

**Files:**
- Modify: `skills/tg-analytic-skill/references/schema.md`

- [ ] **Step 1: Add the three CREATE statements to the "Full schema at a glance" block** — copy them **verbatim** from `_common.py` (the checker normalizes whitespace and `IF NOT EXISTS`, nothing else), after `subscriber_sources`.

- [ ] **Step 2: Add per-table sections** after the `subscriber_sources` section, each restating its CREATE in a ```sql block plus these notes:

````markdown
## `group_messages` — the discussion group, self-contained

Written by the `group` command. **Deliberately overlaps `post_comments`**
(see ADR-0001): comment counts per post → `post_comments`; thread
structure, reactions, per-user engagement → here. Don't count comments
from both.

```sql
<group_messages CREATE, verbatim>
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
- `reactions` — non-paid reaction count at last scan (upserted, not a
  time series).
- `author` — same generated convenience identity as `post_comments`.

## `group_events` — joins & leaves

```sql
<group_events CREATE, verbatim>
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
<group_metrics CREATE, verbatim>
```

- Same idiom as `post_metrics`: one row per `group` run, **`MAX(id)` =
  latest snapshot**, not `MAX(scrape_date)`.
- `group_link`/`group_title` double as the identity record of which
  group this DB's group_* rows came from.
- `members` — participants_count at scan time; drift vs cumulative
  `joins - leaves` reveals event-log gaps.
````

- [ ] **Step 3: Add the canonical CTA-attribution query to "Common joins"**

````markdown
Joins attributable to a post's CTA (window: post publish + N days):

```sql
SELECT COUNT(*) AS joins
FROM group_events e, posts p
WHERE p.id = :post_id
  AND e.kind = 'join'
  AND e.date >= p.date
  AND e.date < datetime(p.date, '+7 days');
```

Thread stats per post (engagement excludes roots — always):

```sql
SELECT gm.thread_post_id AS post_id, COUNT(*) AS replies,
       COUNT(DISTINCT gm.author) AS commenters
FROM group_messages gm
WHERE gm.is_thread_root = 0 AND gm.thread_post_id IS NOT NULL
GROUP BY gm.thread_post_id
ORDER BY replies DESC;
```
````

- [ ] **Step 4: Run the drift guard**

Run: `uv run skills/tg-analytic-skill/scripts/check_schema_doc.py`
Expected: `OK: schema.md matches SCHEMA (13 statements)`

- [ ] **Step 5: Commit**

```bash
git add skills/tg-analytic-skill/references/schema.md
git commit -m "docs(schema): document group_* tables, root-exclusion and CTA-attribution queries"
```

---

### Task 6: `summarize_group` in `_render.py` (TDD)

**Files:**
- Modify: `skills/tg-analytic-skill/scripts/_render.py`
- Test: `tests/test_render_group.py`

Dict shapes (the interface `tg_scrape.py` will build in Task 7):

- `overview`: `{"title", "link", "members", "standalone", "id_range"}`
- `messages`: `[{"id", "date", "author", "is_thread_root", "thread_post_id", "reactions", "text"}]`
- `events`: `[{"kind", "via", "date", "author"}]`
- `threads`: `[{"post_id", "post_link", "snippet", "replies", "commenters", "first_reply_minutes"}]` (empty for standalone)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_render_group.py
from _render import summarize_group


OVERVIEW = {
    "title": "Fastnews chat",
    "link": "https://t.me/fastnewschat",
    "members": 137,
    "standalone": False,
    "id_range": "500..520",
}
MESSAGES = [
    {"id": 500, "date": "2026-06-01T10:00:00+00:00", "author": "channel",
     "is_thread_root": 1, "thread_post_id": 42, "reactions": 30, "text": "post"},
    {"id": 501, "date": "2026-06-01T10:05:00+00:00", "author": "ann",
     "is_thread_root": 0, "thread_post_id": 42, "reactions": 2, "text": "nice"},
    {"id": 502, "date": "2026-06-01T21:00:00+00:00", "author": "bob",
     "is_thread_root": 0, "thread_post_id": None, "reactions": 0, "text": "hello all"},
]
EVENTS = [
    {"kind": "join", "via": "link", "date": "2026-06-01T10:10:00+00:00", "author": "carl"},
    {"kind": "leave", "via": "self", "date": "2026-06-02T09:00:00+00:00", "author": "dan"},
]
THREADS = [
    {"post_id": 42, "post_link": "https://t.me/fastnewsdev/42", "snippet": "post",
     "replies": 1, "commenters": 1, "first_reply_minutes": 5},
]


def test_summary_sections_and_counts(capsys):
    summarize_group("@fastnewsdev", OVERVIEW, MESSAGES, EVENTS, THREADS)
    out = capsys.readouterr().out
    assert "# Group summary: @fastnewsdev" in out
    assert "Fastnews chat" in out and "137" in out
    # roots excluded from message counts: 2 messages (1 thread, 1 chatter)
    assert "Messages: 2 (1 in threads, 1 top-level chatter)" in out
    assert "Joins: 1 (link 1)" in out
    assert "Leaves: 1 (self 1)" in out
    assert "## Activity by hour of day" in out
    assert "## Threads in window" in out
    assert "https://t.me/fastnewsdev/42" in out
    assert "## Engagement" in out
    assert "ann" in out


def test_hour_profile_has_24_rows_and_tz_label(capsys):
    summarize_group("@fastnewsdev", OVERVIEW, MESSAGES, EVENTS, THREADS)
    out = capsys.readouterr().out
    table = out.split("## Activity by hour of day")[1].split("##")[0]
    rows = [l for l in table.splitlines() if l.startswith("| ") and ":00" in l]
    assert len(rows) == 24
    assert "UTC" in out.split("## Activity by hour of day")[1].splitlines()[0]


def test_standalone_omits_threads(capsys):
    ov = dict(OVERVIEW, standalone=True)
    summarize_group("@somegroup", ov, MESSAGES, EVENTS, [])
    out = capsys.readouterr().out
    assert "## Threads in window" not in out


def test_empty_scan(capsys):
    summarize_group("@fastnewsdev", OVERVIEW, [], [], [])
    out = capsys.readouterr().out
    assert "No group messages or events in the scanned window." in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest python -m pytest tests/test_render_group.py -q`
Expected: FAIL — `ImportError: cannot import name 'summarize_group'`

- [ ] **Step 3: Implement `summarize_group`** (append to `_render.py`; `Counter`, `datetime`, `UTC`, `_md_cell` already exist in the module)

```python
def _local_hour(iso: str | None) -> int | None:
    """Hour-of-day in the machine's local timezone (stored dates are UTC)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().hour


def _local_tz_label() -> str:
    """e.g. 'UTC+03:00' — labels the hour table so it's not misread as UTC."""
    offset = datetime.now(UTC).astimezone().strftime("%z")
    return f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"


def _via_breakdown(events: list[dict], kind: str) -> str:
    counts = Counter(e.get("via") or "?" for e in events if e["kind"] == kind)
    total = sum(counts.values())
    if not total:
        return f"{total}"
    detail = ", ".join(f"{via} {n}" for via, n in counts.most_common())
    return f"{total} ({detail})"


def summarize_group(
    label: str,
    overview: dict,
    messages: list[dict],
    events: list[dict],
    threads: list[dict],
) -> None:
    """Print an LLM-oriented summary of a discussion-group scan to stdout.

    `messages` includes thread roots (is_thread_root=1); every engagement
    figure below excludes them — roots carry the channel post's reactions.
    """
    print(f"\n# Group summary: {label}\n")
    if not messages and not events:
        print("No group messages or events in the scanned window.")
        return

    own = [m for m in messages if not m.get("is_thread_root")]
    in_threads = [m for m in own if m.get("thread_post_id") is not None]
    chatter = [m for m in own if m.get("thread_post_id") is None]
    joins = [e for e in events if e["kind"] == "join"]
    leaves = [e for e in events if e["kind"] == "leave"]

    print("## Overview\n")
    members = overview.get("members")
    members_str = f" — {members:,} members" if members is not None else ""
    print(f"- Group: {overview.get('title') or label} ({overview.get('link')}){members_str}")
    dates = sorted(d for m in own for d in [m.get("date")] if d)
    if dates:
        print(f"- Window: {dates[0][:10]} → {dates[-1][:10]}"
              f"  (group-msg ids {overview.get('id_range')})")
    print(f"- Messages: {len(own)} ({len(in_threads)} in threads, "
          f"{len(chatter)} top-level chatter)")
    print(f"- Joins: {_via_breakdown(events, 'join')}  |  "
          f"Leaves: {_via_breakdown(events, 'leave')}  |  "
          f"net {len(joins) - len(leaves):+d}")
    if overview.get("standalone"):
        print("- Standalone group: no linked channel, so no threads.")

    by_day: dict[str, Counter] = {}
    for e in events:
        day = (e.get("date") or "")[:10]
        if day:
            by_day.setdefault(day, Counter())[e["kind"]] += 1
    if by_day:
        print("\n## Joins & leaves by day\n")
        print("| Day | Joins | Leaves |")
        print("|-----|------:|-------:|")
        for day in sorted(by_day):
            c = by_day[day]
            print(f"| {day} | {c['join']} | {c['leave']} |")

    # Hour-of-day profile: all days aggregated, machine-local tz. Three
    # aligned signals so spikes can be compared at a glance.
    joins_h: Counter = Counter()
    msgs_h: Counter = Counter()
    authors_h: dict[int, set] = {}
    for e in joins:
        if (h := _local_hour(e.get("date"))) is not None:
            joins_h[h] += 1
    for m in own:
        if (h := _local_hour(m.get("date"))) is not None:
            msgs_h[h] += 1
            authors_h.setdefault(h, set()).add(m.get("author"))
    print(f"\n## Activity by hour of day ({_local_tz_label()}, machine-local)\n")
    print("| Hour | Joins | Messages | Uniq authors |")
    print("|------|------:|---------:|-------------:|")
    for h in range(24):
        print(f"| {h:02d}:00 | {joins_h[h]} | {msgs_h[h]} "
              f"| {len(authors_h.get(h, ()))} |")

    if threads and not overview.get("standalone"):
        print(f"\n## Threads in window ({len(threads)})\n")
        print("| Post | Replies | Commenters | First reply | Snippet |")
        print("|------|--------:|-----------:|-------------|---------|")
        for t in sorted(threads, key=lambda t: t["replies"], reverse=True):
            first = t.get("first_reply_minutes")
            first_str = f"{first:.0f}m" if first is not None else "—"
            print(f"| {t['post_link'] or t['post_id']} | {t['replies']} "
                  f"| {t['commenters']} | {first_str} | {_md_cell(t.get('snippet'))} |")

    if own:
        print("\n## Engagement\n")
        per_author: Counter = Counter(m.get("author") for m in own)
        reacts: Counter = Counter()
        for m in own:
            reacts[m.get("author")] += m.get("reactions") or 0
        print("| Author | Messages | Reactions received |")
        print("|--------|---------:|-------------------:|")
        for author, n in per_author.most_common(10):
            print(f"| {author} | {n} | {reacts[author]} |")
        days = len({(m.get("date") or "")[:10] for m in own if m.get("date")}) or 1
        print(f"\n- Avg messages/day: {len(own) / days:.1f}")
        top = max(own, key=lambda m: m.get("reactions") or 0)
        if top.get("reactions"):
            print(f"- Most-reacted message: {top['reactions']} reactions — "
                  f"{top.get('author')}: \"{_md_cell(top.get('text'))}\"")
```

- [ ] **Step 4: Run the full test suite**

Run: `uv run --with pytest python -m pytest tests/ -q`
Expected: 22 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_render_group.py skills/tg-analytic-skill/scripts/_render.py
git commit -m "feat(group): render group summary (hourly activity, threads, engagement)"
```

---

### Task 7: `tg_scrape.py` — scan, persist, CLI command

**Files:**
- Modify: `skills/tg-analytic-skill/scripts/tg_scrape.py`

No unit tests for this task (Telethon-facing; the pure logic it delegates to is covered by Tasks 2–3, the rendering by Task 6). Verification is the live run in Task 9.

- [ ] **Step 1: Add imports**

To the existing `telethon.tl.types` import block add `MessageService` and (already present) `PeerChannel`. Below the `_common` import line add:

```python
from _group import (
    GroupEvent,
    auto_forward_post_id,
    classify_service_message,
    thread_post_id_for,
    unresolved_root_refs,
)
```

and extend the `_render` import with `summarize_group`.

- [ ] **Step 2: Add group resolution + DB writers** (new section after the "Scheduled posts" section, before `# CLI`)

```python
# ---------------------------------------------------------------------------
# Discussion-group analytics
# ---------------------------------------------------------------------------


@dataclass
class GroupTarget:
    entity: object          # the group, resolved
    title: str | None
    link: str
    members: int | None
    channel_id: int | None  # linked channel's id; None = standalone


async def resolve_group_target(
    client: TelegramClient, channel: str | None, group: str | None
) -> GroupTarget:
    """Resolve the group to scan from exactly one of --channel/--group.

    --channel: the channel's linked discussion group (error if none).
    --group: the group itself, treated as standalone — if it turns out to
    be linked to a channel, log a notice suggesting --channel and proceed
    (explicit beats clever: never silently redirect to another DB)."""
    if channel:
        ch_entity = await client.get_entity(channel)
        full = await client(GetFullChannelRequest(ch_entity))
        linked = full.full_chat.linked_chat_id
        if not linked:
            log.error(
                "%s has no linked discussion group - nothing to scan", channel
            )
            raise typer.Exit(code=1)
        entity = await client.get_entity(PeerChannel(linked))
        channel_id = ch_entity.id
    else:
        entity = await client.get_entity(group)
        channel_id = None

    g_full = await client(GetFullChannelRequest(entity))
    if group and g_full.full_chat.linked_chat_id:
        log.warning(
            "%s is the discussion group of a channel (id %d) - to get "
            "thread analytics, re-run with --channel <that channel>",
            group, g_full.full_chat.linked_chat_id,
        )
    username = getattr(entity, "username", None)
    link = f"https://t.me/{username}" if username else f"https://t.me/c/{entity.id}"
    return GroupTarget(
        entity=entity,
        title=getattr(entity, "title", None),
        link=link,
        members=g_full.full_chat.participants_count,
        channel_id=channel_id,
    )


def _sender_fields(msg: Message) -> tuple[int | None, str | None, str | None]:
    """(user_id, display name, username) for a message's sender — the same
    extraction get_comments does, shared shape with post_comments."""
    sender = msg.sender
    if sender is None:
        peer = getattr(msg, "from_id", None)
        return getattr(peer, "user_id", None), None, None
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    name = (first + " " + last).strip() or getattr(sender, "title", None)
    return sender.id, name or None, getattr(sender, "username", None)


def upsert_group_messages(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.executemany(
        """
        INSERT INTO group_messages (
            id, date, text, user_id, user_name, user_username,
            reply_to_msg_id, thread_post_id, is_thread_root,
            reactions, media_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            date            = excluded.date,
            text            = excluded.text,
            user_id         = excluded.user_id,
            user_name       = excluded.user_name,
            user_username   = excluded.user_username,
            reply_to_msg_id = excluded.reply_to_msg_id,
            thread_post_id  = excluded.thread_post_id,
            is_thread_root  = excluded.is_thread_root,
            reactions       = excluded.reactions,
            media_type      = excluded.media_type
        """,
        rows,
    )


def upsert_group_events(
    conn: sqlite3.Connection,
    events: list[GroupEvent],
    users: dict[int, tuple[str | None, str | None]],
) -> None:
    conn.executemany(
        """
        INSERT INTO group_events (
            id, date, kind, via, user_id, user_name, user_username
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id, user_id) DO UPDATE SET
            date          = excluded.date,
            kind          = excluded.kind,
            via           = excluded.via,
            user_name     = excluded.user_name,
            user_username = excluded.user_username
        """,
        [
            (e.id, e.date, e.kind, e.via, e.user_id,
             *(users.get(e.user_id) or (None, None)))
            for e in events
        ],
    )


def insert_group_metrics(
    conn: sqlite3.Connection, scrape_date: str, target: GroupTarget
) -> None:
    conn.execute(
        """
        INSERT INTO group_metrics (scrape_date, group_link, group_title, members)
        VALUES (?, ?, ?, ?)
        """,
        (scrape_date, target.link, target.title, target.members),
    )
```

- [ ] **Step 3: Add the scan pipeline** (continues the same section)

```python
async def _resolve_event_users(
    client: TelegramClient, events: list[GroupEvent]
) -> dict[int, tuple[str | None, str | None]]:
    """user_id -> (name, username) for event subjects. Added-user events
    reference users who never sent a message, so iter_messages' entity
    cache may miss them; resolve individually, tolerating dead accounts."""
    users: dict[int, tuple[str | None, str | None]] = {}
    for uid in {e.user_id for e in events if e.user_id is not None}:
        try:
            entity = await client.get_entity(uid)
            first = getattr(entity, "first_name", "") or ""
            last = getattr(entity, "last_name", "") or ""
            users[uid] = (
                (first + " " + last).strip() or None,
                getattr(entity, "username", None),
            )
        except Exception as e:
            log.debug("event user %d unresolvable (%s)", uid, e)
            users[uid] = (None, None)
    return users


def _group_message_row(msg: Message, root_map: dict[int, int]) -> tuple:
    uid, name, username = _sender_fields(msg)
    reactions, stars = count_reactions(msg)
    is_root = msg.id in root_map
    thread_post = (
        root_map[msg.id] if is_root else thread_post_id_for(msg, root_map)
    )
    return (
        msg.id,
        msg.date.isoformat() if msg.date else None,
        msg.text or "",
        uid,
        name,
        username,
        msg.reply_to_msg_id,
        thread_post,
        1 if is_root else 0,
        reactions + stars,
        media_type(msg),
    )


def _load_thread_stats(
    conn: sqlite3.Connection, lo: int, hi: int
) -> list[dict]:
    """Per-thread stats for threads touched in the scanned id window,
    joined to posts for snippet/link and time-to-first-reply."""
    rows = conn.execute(
        """
        SELECT gm.thread_post_id, p.link, substr(COALESCE(p.text, ''), 1, 80),
               p.date, COUNT(*), COUNT(DISTINCT gm.author), MIN(gm.date)
        FROM group_messages gm
        LEFT JOIN posts p ON p.id = gm.thread_post_id
        WHERE gm.is_thread_root = 0
          AND gm.thread_post_id IS NOT NULL
          AND gm.id BETWEEN ? AND ?
        GROUP BY gm.thread_post_id
        """,
        (lo, hi),
    ).fetchall()
    threads = []
    for post_id, link, snippet, post_date, replies, commenters, first in rows:
        minutes = None
        if post_date and first:
            try:
                delta = (
                    datetime.fromisoformat(first)
                    - datetime.fromisoformat(post_date)
                ).total_seconds()
                minutes = max(delta, 0) / 60
            except ValueError:
                pass
        threads.append(
            {
                "post_id": post_id,
                "post_link": link,
                "snippet": snippet,
                "replies": replies,
                "commenters": commenters,
                "first_reply_minutes": minutes,
            }
        )
    return threads


async def scan_group(
    channel: str | None,
    group: str | None,
    output_dir: Path,
    session_file: str,
    limit: int | None,
    offset_id: int,
    offset_date: datetime | None,
    latest: int | None,
) -> None:
    """Scan the discussion group: messages + membership events + a member
    snapshot. Selection semantics mirror `scrape` (same flags, same
    --latest newest-first flip, same inclusive --offset-id)."""
    scrape_date = datetime.now(UTC).isoformat()
    handle = channel or group
    label = channel or group

    if latest is not None:
        reverse, iter_limit = False, latest
        iter_offset_id, iter_offset_date = 0, None
    else:
        reverse, iter_limit = True, limit
        iter_offset_id = offset_id - 1 if offset_id else 0
        iter_offset_date = offset_date

    conn = open_db(output_dir, handle)
    try:
        async with channel_session(session_file) as (client, _):
            target = await resolve_group_target(client, channel, group)
            log.info(
                "authenticated, scanning group %s (%s)",
                target.title or target.link, label,
            )
            raw = [
                m
                async for m in client.iter_messages(
                    target.entity,
                    limit=iter_limit,
                    reverse=reverse,
                    offset_id=iter_offset_id,
                    offset_date=iter_offset_date,
                )
            ]
            log.info("fetched %d group messages", len(raw))

            service = [m for m in raw if isinstance(m, MessageService)]
            ordinary = [m for m in raw if isinstance(m, Message)]

            events = [e for m in service for e in classify_service_message(m)]
            users = await _resolve_event_users(client, events)

            root_map = {
                m.id: pid
                for m in ordinary
                if (pid := auto_forward_post_id(m, target.channel_id))
            }
            # Comments whose thread root fell outside the window: fetch the
            # referenced heads once and keep the ones that are real roots.
            missing = unresolved_root_refs(ordinary, root_map)
            if missing and target.channel_id is not None:
                log.info("resolving %d out-of-window thread heads", len(missing))
                fetched = await client.get_messages(
                    target.entity, ids=sorted(missing)
                )
                extra_roots = [
                    m
                    for m in fetched
                    if isinstance(m, Message)
                    and (pid := auto_forward_post_id(m, target.channel_id))
                    and not root_map.update({m.id: pid})  # update returns None
                ]
                ordinary.extend(extra_roots)

            rows = [_group_message_row(m, root_map) for m in ordinary]
            upsert_group_messages(conn, rows)
            upsert_group_events(conn, events, users)
            insert_group_metrics(conn, scrape_date, target)
            conn.commit()
            log.info(
                "stored %d message(s), %d event(s) in %s",
                len(rows), len(events), db_path_for(output_dir, handle),
            )

        ids = [m.id for m in ordinary] + [e.id for e in events]
        lo, hi = (min(ids), max(ids)) if ids else (0, 0)
        threads = (
            _load_thread_stats(conn, lo, hi)
            if target.channel_id is not None
            else []
        )
    finally:
        conn.close()

    overview = {
        "title": target.title,
        "link": target.link,
        "members": target.members,
        "standalone": target.channel_id is None,
        "id_range": f"{lo}..{hi}" if ids else "—",
    }
    messages = [
        {
            "id": r[0], "date": r[1], "text": r[2],
            "author": r[5] or r[4] or (str(r[3]) if r[3] else None),
            "reply_to_msg_id": r[6], "thread_post_id": r[7],
            "is_thread_root": r[8], "reactions": r[9],
        }
        for r in rows
    ]
    event_dicts = [
        {
            "kind": e.kind, "via": e.via, "date": e.date,
            "author": (users.get(e.user_id) or (None, None))[1]
            or (users.get(e.user_id) or (None, None))[0]
            or (str(e.user_id) if e.user_id else None),
        }
        for e in events
    ]
    summarize_group(label, overview, messages, event_dicts, threads)
```

**Implementation notes for this step (read before coding):**
- `rows`/`ids`/`lo`/`hi`/`target`/`users`/`events` are referenced after the `async with` block — declare nothing inside that block that the post-block code needs without it being assigned on all paths (the `try/finally` keeps `conn` open until after `_load_thread_stats`).
- The `not root_map.update(...)` idiom inside the comprehension is a compact update-and-keep; if it reads too clever during implementation, replace with an explicit loop — behavior over style.
- Empty scan (`raw == []`): `ids` empty → `lo, hi = 0, 0`, threads `[]`, renderer prints the empty-scan line. Must not crash.

- [ ] **Step 4: Add the CLI command** (after `scheduled`, before `login`)

```python
@app.command("group")
def group_cmd(
    channel: Annotated[
        str | None,
        typer.Option(
            help="Channel username - scan its linked discussion group "
            "(rows land in the CHANNEL's DB, threads join to posts)."
        ),
    ] = None,
    group: Annotated[
        str | None,
        typer.Option(
            help="Group username - scan a standalone group (own DB, no "
            "thread linkage). For a group attached to a channel you "
            "analyze, prefer --channel."
        ),
    ] = None,
    output_dir: OutputDirOpt = DEFAULT_OUTPUT_DIR,
    session_file: SessionOpt = DEFAULT_SESSION,
    limit: Annotated[
        int | None,
        typer.Option(
            help="Max messages fetched in the chronological walk. Use with "
            "--offset-id/--offset-date to cap a forward page; for 'N newest' "
            "use --latest instead."
        ),
    ] = None,
    offset_id: Annotated[
        int,
        typer.Option(
            help="Start at this group-message id (inclusive) and walk "
            "forward. 0 = walk from the beginning of history."
        ),
    ] = 0,
    offset_date: Annotated[
        datetime | None,
        typer.Option(
            formats=["%d-%m-%Y", "%d-%m-%Y %H:%M:%S"],
            help="Start after this date and walk forward to newer messages.",
        ),
    ] = None,
    latest: Annotated[
        int | None,
        typer.Option(
            help="Fetch the N most recent group messages (newest-first). "
            "Overrides --limit/--offset-id/--offset-date."
        ),
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """Scan a discussion group: messages, threads, join/leave events.

    Joins/leaves come from the group's service messages (membership
    needed, no admin). Pass exactly one of --channel/--group."""
    if (channel is None) == (group is None):
        typer.echo("Pass exactly one of --channel or --group.", err=True)
        raise typer.Exit(code=2)
    _prepare(session_file, verbose)
    asyncio.run(
        scan_group(
            channel, group, output_dir, session_file,
            limit, offset_id, offset_date, latest,
        )
    )
```

- [ ] **Step 5: Smoke-check the CLI surface (no session needed for --help)**

Run: `uv run skills/tg-analytic-skill/scripts/tg_scrape.py group --help`
Expected: help text with `--channel`, `--group`, the four selection flags.

Run: `uv run skills/tg-analytic-skill/scripts/tg_scrape.py group`
Expected: `Pass exactly one of --channel or --group.` and exit code 2.

- [ ] **Step 6: Run the full test suite (regression)**

Run: `uv run --with pytest python -m pytest tests/ -q`
Expected: 22 passed

- [ ] **Step 7: Commit**

```bash
git add skills/tg-analytic-skill/scripts/tg_scrape.py
git commit -m "feat(group): add group command - scan discussion group messages and membership events"
```

---

### Task 8: Documentation — SKILL.md, CLAUDE.md

**Files:**
- Modify: `skills/tg-analytic-skill/SKILL.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: SKILL.md frontmatter** — extend `description` (after "...views by hour of day from Telegram's stats API.") with: `Also scans the channel's discussion group (or any group the account is in): thread engagement, join/leave events, hourly activity.` Bump `version` to `"1.2"`.

- [ ] **Step 2: SKILL.md CLI list** — in "## CLIs", extend the `tg_scrape.py` command list to `scrape`, `fetch`, `group`, `subscribers`, `views`, `scheduled`.

- [ ] **Step 3: SKILL.md new command section** — insert after the `fetch` pattern section (before "## Other commands"):

````markdown
### 3. Discussion-group analytics — `group`

```
# the channel's linked discussion group (threads join to posts;
# rows land in the CHANNEL's DB)
uv run <skill_dir>/scripts/tg_scrape.py group --channel @name --latest 500

# any standalone group the account is a member of (own DB at
# .tg-analytic/<group>.db; no thread linkage)
uv run <skill_dir>/scripts/tg_scrape.py group --group @name --latest 500
```

Pass **exactly one** of `--channel`/`--group`. For a group that is attached
to a channel you analyze, always use `--channel` — `--group` treats it as
standalone and writes to a separate DB, divorced from the channel's posts
(the script logs a notice when it detects this).

Scans group history into three tables: `group_messages` (every non-service
message, comments included — see the overlap rule in
[references/schema.md](references/schema.md)), `group_events` (joins/leaves
from service messages — needs only membership, **not** admin), and an
append-only `group_metrics` member-count snapshot per run.

Selection flags are the same four as `scrape` (same table above applies:
default to `--latest N`, never bare `--limit`). Incremental refresh:
`--offset-id` from `MAX(id)` over `group_messages`/`group_events`. No media
is downloaded from groups (`media_type` is recorded).

The summary prints joins/leaves by mechanism and by day, an hour-of-day
activity table (joins / messages / unique authors, machine-local timezone —
labeled, don't re-report as UTC), every thread touched in the window
(replies, unique commenters, time-to-first-reply), and top contributors.
CTA-attribution ("did post #X's invite work?") is deliberately NOT
pre-computed — use the canonical query in references/schema.md with the
user's chosen window.

Completeness caveat: Telegram suppresses join/leave service messages in
very large groups. The summary's event counts are what the scan *found*;
cross-check against the `group_metrics.members` trend before claiming
totals.
````

- [ ] **Step 4: SKILL.md Validation section** — add:

```markdown
- After `group` — confirm rows landed:
  ```
  uv run <skill_dir>/scripts/tg_query.py --channel @name \
    "SELECT (SELECT COUNT(*) FROM group_messages) msgs, (SELECT COUNT(*) FROM group_events) events"
  ```
  (`--channel <group>` for a standalone group's DB.) Zero messages on a
  non-empty group means a wrong handle or no membership.
```

- [ ] **Step 5: SKILL.md Common errors table** — add rows:

```markdown
| `... has no linked discussion group` | Channel has comments disabled / no group attached | Only `--group` mode is possible, and only for groups the account can read. |
| `is the discussion group of a channel ... re-run with --channel` (warning, not an error) | `--group` used on an attached group | Re-run with `--channel <channel>` to get thread↔post linkage in the channel's DB. |
```

- [ ] **Step 6: CLAUDE.md** — in the `tg_scrape.py` command table add:

```markdown
| `group` | discussion-group messages + threads + join/leave events → DB; appends a `group_metrics` row per run | membership in the group |
```

And under "Key architecture facts" add:

```markdown
- `group_messages` deliberately duplicates comments that `post_comments`
  also holds (ADR-0001): thread structure/reactions/engagement → query
  `group_messages` (filter `is_thread_root = 0`); per-post comment counts
  → `post_comments`. `group_events` PK is `(id, user_id)` — one add-user
  service message can carry several users.
```

- [ ] **Step 7: Commit**

```bash
git add skills/tg-analytic-skill/SKILL.md CLAUDE.md
git commit -m "docs(skill): document the group command and its query conventions"
```

---

### Task 9: Final verification

- [ ] **Step 1: Full test suite**

Run: `uv run --with pytest python -m pytest tests/ -q`
Expected: 22 passed

- [ ] **Step 2: Schema drift guard**

Run: `uv run skills/tg-analytic-skill/scripts/check_schema_doc.py`
Expected: `OK: schema.md matches SCHEMA (13 statements)`

- [ ] **Step 3: CLI surface**

Run: `uv run skills/tg-analytic-skill/scripts/tg_scrape.py --help`
Expected: `group` listed among the commands.

- [ ] **Step 4: Live run against the real channel** (needs the user's session; run from the project root)

Run: `uv run skills/tg-analytic-skill/scripts/tg_scrape.py group --channel @fastnewsdev --latest 300`
Expected: Markdown summary with Overview / Joins & leaves / Activity by hour / Threads / Engagement; then sanity-check:

```
uv run skills/tg-analytic-skill/scripts/tg_query.py --channel @fastnewsdev \
  "SELECT (SELECT COUNT(*) FROM group_messages) msgs, (SELECT COUNT(*) FROM group_events) events, (SELECT members FROM group_metrics ORDER BY id DESC LIMIT 1) members"
```

- [ ] **Step 5: Cross-check thread linkage** — pick one post id from the summary's Threads table and verify `post_comments` and `group_messages` agree on the comment count:

```
uv run skills/tg-analytic-skill/scripts/tg_query.py --channel @fastnewsdev \
  "SELECT (SELECT COUNT(*) FROM post_comments WHERE post_id = <ID>) channel_view, (SELECT COUNT(*) FROM group_messages WHERE thread_post_id = <ID> AND is_thread_root = 0) group_view"
```

Expected: equal counts (small drift acceptable only if the scans ran at different times).
