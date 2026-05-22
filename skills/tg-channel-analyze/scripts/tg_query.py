# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
import argparse
import logging
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path("data")

# Strip leading `-- line comments` and `/* block comments */` so we can inspect
# the first real keyword. We don't try to parse strings - any leading SELECT
# inside a string literal would still satisfy the check, but that's harmless
# (the engine layer is read-only too; this guard is a clearer early error).
_LINE_COMMENT = re.compile(r"^\s*--[^\n]*\n?")
_BLOCK_COMMENT = re.compile(r"^\s*/\*.*?\*/", re.DOTALL)


def _strip_leading_comments(sql: str) -> str:
    prev = None
    cur = sql.lstrip()
    while prev != cur:
        prev = cur
        cur = _LINE_COMMENT.sub("", cur, count=1).lstrip()
        cur = _BLOCK_COMMENT.sub("", cur, count=1).lstrip()
    return cur


def validate_read_only(sql: str) -> None:
    """Reject anything that isn't a single SELECT/WITH query.

    Belt-and-braces alongside `?mode=ro`: gives a clear error before the
    engine sees a destructive verb (INSERT/UPDATE/DELETE/DROP/ATTACH/PRAGMA
    etc.), and catches multi-statement payloads even though sqlite3.execute
    only runs the first one."""
    body = _strip_leading_comments(sql)
    first = body.split(None, 1)[0].upper() if body else ""
    if first not in ("SELECT", "WITH"):
        raise ValueError(
            f"only SELECT or WITH queries are allowed (got '{first or '<empty>'}')"
        )
    # Reject obvious multi-statement payloads like `SELECT 1; DROP TABLE posts`.
    # A trailing single `;` is fine. A semicolon followed by more SQL is not.
    trimmed = body.rstrip().rstrip(";").rstrip()
    if ";" in trimmed:
        raise ValueError("multi-statement queries are not allowed")


def db_path_for(output_dir: Path, channel: str) -> Path:
    """One DB file per channel, e.g. data/fastnewsdev.db."""
    safe = channel.lstrip("@").replace("/", "_") or "channel"
    return output_dir / f"{safe}.db"


def _format_cell(value, truncate: bool = True) -> str:
    if value is None:
        return ""
    text = str(value).replace("|", "\\|").replace("\n", " ")
    if truncate and len(text) > 200:
        text = text[:197] + "..."
    return text


def query(sql: str, channel: str, output_dir: Path, limit: int, no_truncate: bool) -> int:
    """Run a read-only SQL query against the channel's SQLite DB and print a Markdown table.

    The DB is opened in read-only mode, so writes and schema changes are
    rejected by SQLite itself - safe to expose to LLM-generated SQL."""
    try:
        validate_read_only(sql)
    except ValueError as e:
        log.error("rejected query: %s", e)
        return 1

    db_path = db_path_for(output_dir, channel)
    if not db_path.exists():
        log.error("database not found at %s", db_path)
        return 1

    uri = f"file:{db_path}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        try:
            cursor = conn.execute(sql)
        except sqlite3.DatabaseError as e:
            log.error("query failed: %s", e)
            return 1
        columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchall()

    if not columns:
        print("(query returned no columns)")
        return 0

    truncated = limit and len(rows) > limit
    visible = rows[:limit] if limit else rows

    print("| " + " | ".join(columns) + " |")
    print("| " + " | ".join("---" for _ in columns) + " |")
    for row in visible:
        print("| " + " | ".join(_format_cell(v, truncate=not no_truncate) for v in row) + " |")

    print(f"\n_{len(rows)} row(s)" + (f", showing {limit}_" if truncated else "_"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a read-only SQL query against data/<channel>.db and print a Markdown table.",
    )
    parser.add_argument("sql", help="SQL SELECT statement to run against the channel DB.")
    parser.add_argument(
        "--channel",
        required=True,
        help="Channel username; picks data/<channel>.db (required).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory containing the per-channel DBs (default: %(default)s).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max rows to print, 0 = unlimited (default: %(default)s).",
    )
    parser.add_argument(
        "--no-truncate",
        action="store_true",
        help="Disable per-cell text truncation (default truncates at 200 chars).",
    )
    args = parser.parse_args()
    return query(
        sql=args.sql,
        channel=args.channel,
        output_dir=args.output_dir,
        limit=args.limit,
        no_truncate=args.no_truncate,
    )


if __name__ == "__main__":
    sys.exit(main())