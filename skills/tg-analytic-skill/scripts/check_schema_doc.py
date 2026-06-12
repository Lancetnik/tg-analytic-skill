# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Guard against drift between SCHEMA (the source of truth, in _common.py)
and the DDL that references/schema.md restates for the SQL-writing agent.

Run after editing either side:

    uv run skills/tg-analytic-skill/scripts/check_schema_doc.py

Exits 0 when every statement matches (modulo whitespace and IF NOT EXISTS),
1 with a per-statement diff otherwise. The doc's "Full schema at a glance"
block plus its per-table blocks are all checked - each must restate its
statement exactly.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import SCHEMA  # noqa: E402

SCHEMA_MD = Path(__file__).resolve().parent.parent / "references" / "schema.md"


def normalize(stmt: str) -> str:
    """Whitespace- and IF NOT EXISTS-insensitive form of one DDL statement."""
    stmt = re.sub(r"\bIF NOT EXISTS\b\s*", "", stmt, flags=re.IGNORECASE)
    return " ".join(stmt.split()).rstrip(";").strip()


def statements(sql: str) -> set[str]:
    return {normalize(s) for s in sql.split(";") if normalize(s)}


def main() -> int:
    truth = statements(SCHEMA)

    doc = SCHEMA_MD.read_text(encoding="utf-8")
    doc_sql = "\n".join(re.findall(r"```sql\n(.*?)```", doc, flags=re.DOTALL))
    # Per-table blocks repeat statements from the glance block; common-join
    # examples are SELECTs - keep only CREATE statements.
    documented = {s for s in statements(doc_sql) if s.upper().startswith("CREATE")}

    missing = truth - documented
    stale = documented - truth
    if not missing and not stale:
        print(f"OK: schema.md matches SCHEMA ({len(truth)} statements)")
        return 0

    for s in sorted(missing):
        print(f"NOT IN schema.md:\n  {s}\n", file=sys.stderr)
    for s in sorted(stale):
        print(f"STALE in schema.md (not in SCHEMA):\n  {s}\n", file=sys.stderr)
    print("Fix references/schema.md (or _common.py SCHEMA) and re-run.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
