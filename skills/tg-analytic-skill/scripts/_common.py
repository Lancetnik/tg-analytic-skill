"""Shared data home: runtime paths, the SQLite schema, and DB open helpers.

Imported by both tg_scrape.py and tg_query.py (PEP-723 scripts resolve
same-directory imports because the script's directory is on sys.path).
Must stay stdlib-only so tg_query.py keeps its empty-dependencies property.

The SCHEMA constant below is the single source of truth for the DB layout.
references/schema.md restates it for the SQL-writing agent — run
scripts/check_schema_doc.py after editing either to catch drift.
"""

import sqlite3
from pathlib import Path

# Runtime state anchors on the *current working directory* (the project root
# the user launches from), never on the skill's install location.
DATA_DIR = Path.cwd() / ".tg-analytic"
DEFAULT_OUTPUT_DIR = DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
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

CREATE TABLE IF NOT EXISTS post_attachments (
    post_id        INTEGER NOT NULL,
    attachment_id  INTEGER NOT NULL,
    link           TEXT,
    media_type     TEXT,
    photo_path     TEXT,
    PRIMARY KEY (post_id, attachment_id)
);

CREATE TABLE IF NOT EXISTS post_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         INTEGER NOT NULL,
    scrape_date     TEXT    NOT NULL,
    views           INTEGER,
    forwards        INTEGER,
    reactions       INTEGER,
    stars           INTEGER,
    comments_count  INTEGER,
    public_forwards_count INTEGER
);

CREATE INDEX IF NOT EXISTS idx_post_metrics_post
    ON post_metrics(post_id);

CREATE TABLE IF NOT EXISTS post_comments (
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

CREATE TABLE IF NOT EXISTS public_channels (
    link         TEXT PRIMARY KEY,
    name         TEXT,
    description  TEXT,
    subscribers  INTEGER,
    last_seen    TEXT
);

CREATE TABLE IF NOT EXISTS public_shares (
    post_id         INTEGER NOT NULL,
    forwarder_link  TEXT    NOT NULL,
    msg_link        TEXT    NOT NULL,
    first_seen      TEXT,
    PRIMARY KEY (post_id, forwarder_link, msg_link)
);

CREATE TABLE IF NOT EXISTS subscribers (
    date     TEXT PRIMARY KEY,
    total    INTEGER,
    joins    INTEGER,
    leaves   INTEGER
);

CREATE TABLE IF NOT EXISTS subscriber_sources (
    date     TEXT    NOT NULL,
    source   TEXT    NOT NULL,
    joins    INTEGER,
    PRIMARY KEY (date, source)
);

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
"""


def db_path_for(output_dir: Path, channel: str) -> Path:
    """One DB file per channel, e.g. .tg-analytic/fastnewsdev.db."""
    safe = channel.lstrip("@").replace("/", "_") or "channel"
    return output_dir / f"{safe}.db"


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Back-fill columns added to SCHEMA after a DB file was created.

    CREATE TABLE IF NOT EXISTS never alters an existing table, so new columns
    must be ALTERed in here. Idempotent: each ALTER runs only when its column
    is absent. `author` is a generated VIRTUAL column (computed on read, no row
    rewrite) — single human-readable commenter identity so LLM-generated SQL
    can say `GROUP BY author` without knowing the user_* split. Needs SQLite
    >= 3.31 (2020); CPython 3.10+ bundles newer. table_xinfo, not table_info:
    only the former lists generated columns, and missing `author` here would
    re-ALTER it on every open."""
    cols = {row[1] for row in conn.execute("PRAGMA table_xinfo(post_comments)")}
    if "author" not in cols:
        conn.execute(
            "ALTER TABLE post_comments ADD COLUMN author TEXT GENERATED ALWAYS AS "
            "(COALESCE(user_username, user_name, CAST(user_id AS TEXT))) VIRTUAL"
        )
        conn.commit()


def open_db(output_dir: Path, channel: str) -> sqlite3.Connection:
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path_for(output_dir, channel))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    return conn
