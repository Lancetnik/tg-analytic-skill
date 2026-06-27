# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "telethon>=1.36,<2",
#     "python-dotenv>=1.0",
#     "typer>=0.12,<1",
#     "mistune>=3.0",
# ]
# ///
"""Publish-side CLI: queue a future channel post.

The skill's one *write* path, kept in its own script so "this code can post"
is auditable at the file level (the read/scrape/query scripts never publish).
See docs/adr/0003.

Pipeline: Markdown --(_md2entities)--> plain text + Telethon MessageEntity list
--> client.send_message(schedule). Scheduling is an MTProto/user-client feature,
so this rides the same Telethon session as the scrapers — the Bot API cannot
schedule.
"""
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from telethon.tl.functions.messages import GetScheduledMessagesRequest
from telethon.tl.types import Message

from utils._common import DATA_DIR
from utils._md2entities import render as render_markdown
from utils._render import summarize_schedule
from utils._tg import DEFAULT_SESSION, _require_session, channel_session

load_dotenv(DATA_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("telethon").setLevel(logging.WARNING)
# Scheduled-message edits (reschedule/edit) trigger a benign Telethon WARNING
# ("No random_id in EditMessageRequest ... to map to") that dumps the whole
# Updates object — the edit still applies. Mute that one logger; real failures
# raise exceptions, not warnings.
logging.getLogger("telethon.client.messageparse").setLevel(logging.ERROR)
log = logging.getLogger(__name__)

UTC = timezone.utc

# Hardcoded on purpose, with no CLI flag or env override: the guard exists to
# stop the *agent* driving this CLI from scheduling a post too soon. A
# configurable floor the agent could pass would be the agent holding its own
# leash. The human owner can still edit this constant. See docs/adr/0003.
MIN_LEAD = timedelta(hours=1)

app = typer.Typer(help="Publish to a Telegram channel: schedule / reschedule / edit posts.")


@app.callback()
def _main() -> None:
    """Keep the subcommand name required even with a single command, so the
    CLI reads `tg_publish.py schedule ...` and stays open to future verbs."""


def _read_body(path: str | None) -> str:
    """Read the Markdown body from a file, or from stdin when `path` is None/`-`.

    stdin keeps the agent from writing a temp file just to strip a draft's
    metainfo: it produces the clean body and pipes it via a quoted heredoc,
    which passes backticks/`$`/quotes verbatim (no shell escaping). The TTY
    guard turns a bare interactive run into a clear message, not a silent hang."""
    if path in (None, "-"):
        if sys.stdin.isatty():
            typer.echo(
                "No --file given and stdin is a terminal. Pass --file PATH, or "
                "pipe the body, e.g. `... --file - <<'EOF'`.",
                err=True,
            )
            raise typer.Exit(code=2)
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"Cannot read --file {path!r}: {exc}", err=True)
        raise typer.Exit(code=2) from None


def _render_markdown(path: str | None) -> tuple[str, list]:
    """Markdown from --file or stdin -> (plain text, Telethon entities)."""
    text, entities = render_markdown(_read_body(path))
    if not text.strip():
        src = "stdin" if path in (None, "-") else f"--file {path!r}"
        typer.echo(f"{src} renders to an empty post.", err=True)
        raise typer.Exit(code=2)
    return text, entities


def _parse_when(at: str) -> datetime:
    """ISO-8601-with-offset -> aware datetime, enforcing the lead-time floor.

    Naive (offset-less) values are rejected: for a *published* post, guessing
    the timezone could place it an hour off and silently defeat the floor."""
    normalized = at.strip()
    if normalized.endswith(("Z", "z")):  # 3.10's fromisoformat rejects 'Z'
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        typer.echo(
            f"Invalid --at {at!r}: use ISO-8601 with an offset, e.g. "
            "2026-06-27T18:00:00+03:00.",
            err=True,
        )
        raise typer.Exit(code=2) from None
    if dt.tzinfo is None:
        typer.echo(
            f"--at {at!r} has no UTC offset. Naive times are ambiguous for a "
            "published post — include one, e.g. 2026-06-27T18:00:00+03:00.",
            err=True,
        )
        raise typer.Exit(code=2)
    earliest = datetime.now(UTC) + MIN_LEAD
    if dt < earliest:
        local_earliest = earliest.astimezone(dt.tzinfo).isoformat(timespec="minutes")
        typer.echo(
            f"--at {dt.isoformat()} is too soon: posts must be scheduled at "
            f"least 1 hour ahead (earliest {local_earliest}).",
            err=True,
        )
        raise typer.Exit(code=1)
    return dt


async def schedule_post(
    channel: str,
    text: str,
    entities: list,
    when: datetime,
    session_file: str,
) -> None:
    async with channel_session(session_file, channel) as (client, entity):
        log.info("authenticated, scheduling post to %s for %s", channel, when.isoformat())
        msg = await client.send_message(
            entity,
            text,
            formatting_entities=entities,
            schedule=when,
        )
    summarize_schedule(
        channel,
        {
            "id": msg.id,
            "date": msg.date.astimezone(UTC).isoformat() if msg.date else None,
            "requested": when.isoformat(),
            "text": text,
            "entities": len(entities),
        },
    )


async def _get_scheduled(client, entity, msg_id: int) -> Message:
    """Fetch one post from the channel's scheduled queue by its sched-msg id.

    One round-trip via GetScheduledMessages (no full-history scan). Exits 1
    with an actionable message if nothing matches — the id is most likely
    stale (the post published, or was already removed)."""
    result = await client(GetScheduledMessagesRequest(peer=entity, id=[msg_id]))
    found = [
        m
        for m in getattr(result, "messages", [])
        if isinstance(m, Message) and m.id == msg_id
    ]
    if not found:
        typer.echo(
            f"No scheduled post #{msg_id} in the queue. List the queue with "
            "`tg_scrape.py scheduled --channel <chan>`.",
            err=True,
        )
        raise typer.Exit(code=1)
    return found[0]


async def reschedule_post(
    channel: str, msg_id: int, when: datetime, session_file: str
) -> None:
    async with channel_session(session_file, channel) as (client, entity):
        existing = await _get_scheduled(client, entity, msg_id)
        log.info("rescheduling post #%d in %s to %s", msg_id, channel, when.isoformat())
        # text=None -> Telegram keeps the body and entities, only moves the time.
        # edit_message returns None for scheduled edits (Telethon can't map the
        # UpdateNewScheduledMessage response to a Message), so build the summary
        # from known inputs: the id is stable and the new time is `when`.
        await client.edit_message(entity, msg_id, schedule=when)
    summarize_schedule(
        channel,
        {
            "id": msg_id,
            "date": when.astimezone(UTC).isoformat(),
            "requested": when.isoformat(),
            "text": existing.message or "",
            "entities": None,
        },
        action="Rescheduled",
    )


async def edit_post(
    channel: str, msg_id: int, text: str, entities: list, session_file: str
) -> None:
    async with channel_session(session_file, channel) as (client, entity):
        existing = await _get_scheduled(client, entity, msg_id)
        # Re-send the existing schedule date: it both keeps the post in the
        # scheduled queue and is the flag that tells Telegram this edit targets
        # the scheduled message (not a published one with the same id).
        when = existing.date
        log.info("editing scheduled post #%d in %s (time unchanged)", msg_id, channel)
        # Like reschedule, edit_message returns None for scheduled edits; the id
        # and time are unchanged, so report from known values.
        await client.edit_message(
            entity, msg_id, text, formatting_entities=entities, schedule=when
        )
    summarize_schedule(
        channel,
        {
            "id": msg_id,
            "date": when.astimezone(UTC).isoformat() if when else None,
            "requested": None,
            "text": text,
            "entities": len(entities),
        },
        action="Edited",
    )


ChannelOpt = Annotated[
    str,
    typer.Option(help="Telegram channel username, required (you need post rights)."),
]
FileOpt = Annotated[
    str | None,
    typer.Option(
        help="Path to the Markdown file with the post body. Omit (or pass '-') "
        "to read the body from stdin, e.g. `--file - <<'EOF' ... EOF`."
    ),
]
AtOpt = Annotated[
    str,
    typer.Option(
        help="When to publish, ISO-8601 with a UTC offset "
        "(e.g. 2026-06-27T18:00:00+03:00). Must be at least 1 hour ahead."
    ),
]
IdOpt = Annotated[
    int,
    typer.Option(help="Scheduled-message id, from `tg_scrape.py scheduled`."),
]
SessionOpt = Annotated[str, typer.Option(help="Telethon session file name.")]


@app.command("schedule")
def schedule(
    channel: ChannelOpt,
    at: AtOpt,
    file: FileOpt = None,
    session_file: SessionOpt = DEFAULT_SESSION,
) -> None:
    """Queue a Markdown post to publish at a future time.

    Body is Markdown rendered straight to Telegram entities (_md2entities). The
    post must be scheduled at least 1 hour ahead; scheduled posts are not
    persisted (their ids differ from published ids and carry no engagement)."""
    when = _parse_when(at)
    text, entities = _render_markdown(file)
    _require_session(session_file)
    asyncio.run(schedule_post(channel, text, entities, when, session_file))


@app.command("reschedule")
def reschedule(
    channel: ChannelOpt,
    id: IdOpt,
    at: AtOpt,
    session_file: SessionOpt = DEFAULT_SESSION,
) -> None:
    """Move an existing scheduled post to a new time; body unchanged.

    Same 1-hour floor as `schedule` (it sets a new publish time). Identify the
    post by its `sched-msg` id from `tg_scrape.py scheduled`."""
    when = _parse_when(at)
    _require_session(session_file)
    asyncio.run(reschedule_post(channel, id, when, session_file))


@app.command("edit")
def edit(
    channel: ChannelOpt,
    id: IdOpt,
    file: FileOpt = None,
    session_file: SessionOpt = DEFAULT_SESSION,
) -> None:
    """Replace the body of an existing scheduled post; publish time unchanged.

    Reads the new body from --file or stdin and renders it the same way
    `schedule` does. No 1-hour floor check — editing text never moves the
    publish time. Identify the post by its `sched-msg` id from `scheduled`."""
    text, entities = _render_markdown(file)
    _require_session(session_file)
    asyncio.run(edit_post(channel, id, text, entities, session_file))


if __name__ == "__main__":
    app()
