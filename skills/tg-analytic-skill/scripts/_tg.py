"""Telethon session + credential plumbing shared by the write/read scripts.

Lives apart from `_common.py` on purpose: `_common` is stdlib-only so
`tg_query.py` keeps its empty-dependencies property, whereas everything here
imports Telethon. Both `tg_scrape.py` (reads) and `tg_publish.py` (the one
write path) import these helpers so the connect/auth dance has a single home.
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

import typer
from telethon import TelegramClient

from _common import DATA_DIR

DEFAULT_SESSION_FILE = DATA_DIR / "session.session"
DEFAULT_SESSION = str(DEFAULT_SESSION_FILE)

# `login` lives in tg_scrape.py; point users there regardless of which script
# tripped the missing-session check. Same directory as this module.
_LOGIN_SCRIPT = Path(__file__).resolve().parent / "tg_scrape.py"


def _credentials() -> tuple[int, str, str]:
    """Read TG_API_ID / TG_API_HASH / TG_PHONE lazily, at connect time.

    Reading them at import time would crash even `--help` with a bare KeyError
    when .tg-analytic/.env doesn't exist yet; deferring turns that into a
    clear, actionable message on the first command that actually connects."""
    try:
        return (
            int(os.environ["TG_API_ID"]),
            os.environ["TG_API_HASH"],
            os.environ["TG_PHONE"],
        )
    except KeyError as e:
        typer.echo(
            f"Missing {e.args[0]} - put TG_API_ID/TG_API_HASH/TG_PHONE in "
            f"{DATA_DIR / '.env'} (see the skill's .env.example).",
            err=True,
        )
        raise typer.Exit(code=1) from None


def make_client(session_file: str) -> TelegramClient:
    api_id, api_hash, _ = _credentials()
    return TelegramClient(str(session_file), api_id, api_hash)


def _require_session(session_file: str) -> None:
    """Fail fast if no Telethon session exists.

    Auth needs an interactive TTY for the SMS code prompt, so it cannot run
    inside a Bash subprocess. Surface that with a clear message instead of
    deadlocking on input()."""
    if not Path(session_file).exists():
        # Print the real path — the skill installs under varying roots
        # (.claude/skills/, .agents/skills/, the source repo), so a hardcoded
        # relative path would point nowhere for most users.
        typer.echo(
            f"Telegram session not found at {session_file}\n"
            f"Run `uv run {_LOGIN_SCRIPT} login` in your own "
            "terminal first (interactive — needs an SMS code).",
            err=True,
        )
        raise typer.Exit(code=1)


@asynccontextmanager
async def channel_session(session_file: str, channel: str | None = None):
    """Connected Telegram client with an owned lifecycle.

    Yields `(client, entity)` — entity resolved when `channel` is given, None
    otherwise (login). One home for the connect / resolve / disconnect dance
    every command previously copied; the client never crosses this seam
    unmanaged."""
    client = make_client(session_file)
    await client.start(phone=_credentials()[2])
    try:
        entity = await client.get_entity(channel) if channel else None
        yield client, entity
    finally:
        await client.disconnect()
