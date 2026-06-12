# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "telethon>=1.36,<2",
#     "python-dotenv>=1.0",
#     "typer>=0.12,<1",
# ]
# ///
import asyncio
import json
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetScheduledHistoryRequest
from telethon.tl.functions.stats import (
    GetBroadcastStatsRequest,
    GetMessagePublicForwardsRequest,
    LoadAsyncGraphRequest,
)
from telethon.tl.types import (
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    PeerChannel,
    PublicForwardMessage,
    ReactionPaid,
    StatsGraph,
    StatsGraphAsync,
)

from _common import DATA_DIR, DEFAULT_OUTPUT_DIR, db_path_for, open_db
from _render import (
    summarize_scheduled,
    summarize_scrape,
    summarize_subscribers,
    summarize_views,
)

load_dotenv(DATA_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
# Telethon's network chatter (Connecting/Disconnecting) is pure noise for an
# LLM consuming the output - keep only warnings/errors from it.
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# `datetime.UTC` is 3.11+; alias it from `timezone.utc` for 3.10 compatibility.
UTC = timezone.utc

# Per-post progress prints every Nth post at INFO; per-post lines go to DEBUG.
PROGRESS_EVERY = 50


def _log_progress(done: int, total: int, current: str, id_range: str) -> None:
    log.debug("[%d/%d] processed %s", done, total, current)
    if done == total or done % PROGRESS_EVERY == 0:
        log.info("[%d/%d] processed (ids: %s)", done, total, id_range)


DEFAULT_SESSION_FILE = DATA_DIR / "session.session"


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
        typer.echo(
            f"Telegram session not found at {session_file}\n"
            "Run `uv run scripts/tg_scrape.py login` in your own terminal first "
            "(interactive — needs an SMS code).",
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


@dataclass
class ForwardInfo:
    msg_link: str
    channel_link: str
    peer: object


@dataclass
class ChannelInfo:
    name: str | None
    description: str | None
    subscribers: int | None


@dataclass
class ChannelRecord:
    peer: object
    post_ids: list[int] = field(default_factory=list)


def media_type(msg: Message) -> str | None:
    if not msg.media:
        return None
    if isinstance(msg.media, MessageMediaPhoto):
        return "photo"
    if isinstance(msg.media, MessageMediaDocument):
        return "document"
    return type(msg.media).__name__


def tme_link(channel: str, msg_id: int) -> str:
    return f"https://t.me/{channel.lstrip('@')}/{msg_id}"


def extract_tags(text: str) -> list[str]:
    return re.findall(r"(?<!\S)#(\w+)", text)


def count_reactions(msg: Message) -> tuple[int, int]:
    reactions = stars = 0
    if msg.reactions:
        for r in msg.reactions.results:
            if isinstance(r.reaction, ReactionPaid):
                stars += r.count
            else:
                reactions += r.count
    return reactions, stars


def group_albums(messages: list[Message]) -> list[list[Message]]:
    """Group album members by grouped_id; standalone posts become singletons.

    Shared by the persist pipeline and `scheduled` so the album invariant
    (one logical post per grouped_id) lives in one place."""
    groups: dict[int, list[Message]] = {}
    standalone: list[Message] = []
    for msg in messages:
        if msg.grouped_id:
            groups.setdefault(msg.grouped_id, []).append(msg)
        else:
            standalone.append(msg)
    return [[m] for m in standalone] + list(groups.values())


async def get_forward_source(
    client: TelegramClient, msg: Message
) -> ForwardInfo | None:
    """If `msg` is itself a forward of a channel post, resolve the source.

    Returns the source channel as a ForwardInfo so the caller can register it
    in the same channel_map used for outbound forwarders - the source then
    gets persisted to `public_channels` alongside everything else.
    Returns None for non-channel forwards (user/chat) and hidden senders."""
    fwd = msg.fwd_from
    if not fwd or not getattr(fwd, "from_id", None):
        return None
    peer = fwd.from_id
    if not isinstance(peer, PeerChannel):
        return None

    username: str | None = None
    try:
        entity = await client.get_entity(peer)
        username = getattr(entity, "username", None)
    except Exception as e:
        # Private/restricted channels still leave us with channel_id, so we
        # can persist them under the `t.me/c/<id>` form.
        log.error("msg %d: failed to resolve fwd_from entity (%s)", msg.id, e)

    ch_link = (
        f"https://t.me/{username}"
        if username
        else f"https://t.me/c/{peer.channel_id}"
    )
    src_msg_id = getattr(fwd, "channel_post", None)
    return ForwardInfo(
        msg_link=f"{ch_link}/{src_msg_id}" if src_msg_id else "",
        channel_link=ch_link,
        peer=peer,
    )


async def get_public_forwards(
    client: TelegramClient, channel_entity, msg_id: int
) -> list[ForwardInfo]:
    result_list: list[ForwardInfo] = []
    try:
        result = await client(
            GetMessagePublicForwardsRequest(
                channel=channel_entity,
                msg_id=msg_id,
                offset="",
                limit=100,
            )
        )
        for fwd in result.forwards:
            if not isinstance(fwd, PublicForwardMessage):
                continue
            peer = fwd.message.peer_id
            try:
                entity = await client.get_entity(peer)
                username = getattr(entity, "username", None)
                ch_link = (
                    f"https://t.me/{username}"
                    if username
                    else f"https://t.me/c/{peer.channel_id}"
                )
                result_list.append(
                    ForwardInfo(
                        msg_link=f"{ch_link}/{fwd.message.id}",
                        channel_link=ch_link,
                        peer=peer,
                    )
                )
            except Exception as e:
                log.error("msg %d: failed to resolve forward peer (%s)", msg_id, e)
        if result_list:
            log.debug("msg %d: %d public forward(s)", msg_id, len(result_list))
    except Exception as e:
        log.error("msg %d: public forwards request failed (%s)", msg_id, e)
    return result_list


async def get_comments(
    client: TelegramClient, channel_entity, msg: Message
) -> list[dict]:
    if not (msg.replies and msg.replies.replies):
        return []
    comments = []
    try:
        async for c in client.iter_messages(channel_entity, reply_to=msg.id):
            if not isinstance(c, Message):
                continue
            sender = c.sender
            author = {"id": None, "name": None, "username": None}
            if sender:
                first = getattr(sender, "first_name", "") or ""
                last = getattr(sender, "last_name", "") or ""
                author = {
                    "id": sender.id,
                    "name": (first + " " + last).strip() or None,
                    "username": getattr(sender, "username", None),
                }
            comments.append(
                {
                    "id": c.id,
                    "date": c.date.isoformat() if c.date else None,
                    "text": c.text or "",
                    "author": author,
                }
            )
    except Exception as e:
        log.error("msg %d: failed to fetch comments (%s)", msg.id, e)
    comments.sort(key=lambda c: c["id"])
    if comments:
        log.debug("msg %d: %d comment(s)", msg.id, len(comments))
    return comments


async def get_channel_info(client: TelegramClient, peer) -> ChannelInfo:
    try:
        full = await client(GetFullChannelRequest(peer))
        return ChannelInfo(
            name=full.chats[0].title if full.chats else None,
            description=full.full_chat.about or None,
            subscribers=full.full_chat.participants_count,
        )
    except Exception as e:
        log.error("failed to get channel info (%s)", e)
        return ChannelInfo(name=None, description=None, subscribers=None)


async def download_photo(
    client: TelegramClient, msg: Message, media_dir: Path, with_media: bool
) -> str | None:
    if not with_media or not isinstance(msg.media, MessageMediaPhoto):
        return None
    media_dir.mkdir(parents=True, exist_ok=True)
    dest = media_dir / f"{msg.id}.jpg"
    if dest.exists():
        log.debug("msg %d: photo already cached", msg.id)
        return str(dest)
    log.debug("msg %d: downloading photo", msg.id)
    path = await client.download_media(msg, file=str(dest))
    return str(path) if path else None


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------


def upsert_post(
    conn: sqlite3.Connection,
    channel: str,
    msg: Message,
    attachments: list[tuple[int, str, str | None, str | None]],
    forwarder_from_channel: str | None = None,
) -> None:
    text = msg.text or ""
    tags = extract_tags(text)
    conn.execute(
        """
        INSERT INTO posts (
            id, link, date, text, edit_date,
            reply_to_msg_id, tags, grouped_id, forwarder_from_channel
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            link                   = excluded.link,
            date                   = excluded.date,
            text                   = excluded.text,
            edit_date              = excluded.edit_date,
            reply_to_msg_id        = excluded.reply_to_msg_id,
            tags                   = excluded.tags,
            grouped_id             = excluded.grouped_id,
            forwarder_from_channel = excluded.forwarder_from_channel
        """,
        (
            msg.id,
            tme_link(channel, msg.id),
            msg.date.isoformat() if msg.date else None,
            text,
            (
                msg.edit_date.isoformat()
                if msg.edit_date and msg.edit_date != msg.date
                else None
            ),
            msg.reply_to_msg_id,
            json.dumps(tags, ensure_ascii=False) if tags else None,
            msg.grouped_id,
            forwarder_from_channel,
        ),
    )

    # Replace attachments wholesale - cheaper than diffing and matches re-scrape semantics.
    conn.execute(
        "DELETE FROM post_attachments WHERE post_id = ?",
        (msg.id,),
    )
    conn.executemany(
        """
        INSERT INTO post_attachments (
            post_id, attachment_id, link, media_type, photo_path
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (msg.id, att_id, link, mtype, photo)
            for att_id, link, mtype, photo in attachments
        ],
    )


def insert_metrics(
    conn: sqlite3.Connection,
    msg: Message,
    scrape_date: str,
    comments_count: int,
    public_forwards_count: int,
) -> None:
    reactions, stars = count_reactions(msg)
    conn.execute(
        """
        INSERT INTO post_metrics (
            post_id, scrape_date, views, forwards,
            reactions, stars, comments_count, public_forwards_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            msg.id,
            scrape_date,
            msg.views,
            msg.forwards,
            reactions,
            stars,
            comments_count,
            public_forwards_count,
        ),
    )


def replace_comments(
    conn: sqlite3.Connection,
    post_id: int,
    comments: list[dict],
) -> None:
    conn.execute(
        "DELETE FROM post_comments WHERE post_id = ?",
        (post_id,),
    )
    conn.executemany(
        """
        INSERT INTO post_comments (
            post_id, id, date, text,
            user_id, user_name, user_username
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                post_id,
                c["id"],
                c["date"],
                c["text"],
                c["author"]["id"],
                c["author"]["name"],
                c["author"]["username"],
            )
            for c in comments
        ],
    )


def upsert_public_shares(
    conn: sqlite3.Connection,
    post_id: int,
    forwards: list[ForwardInfo],
    seen_at: str,
) -> None:
    conn.executemany(
        """
        INSERT INTO public_shares (
            post_id, forwarder_link, msg_link, first_seen
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(post_id, forwarder_link, msg_link) DO NOTHING
        """,
        [(post_id, f.channel_link, f.msg_link, seen_at) for f in forwards],
    )


def upsert_public_channel(
    conn: sqlite3.Connection, link: str, info: ChannelInfo, seen_at: str
) -> None:
    conn.execute(
        """
        INSERT INTO public_channels (link, name, description, subscribers, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(link) DO UPDATE SET
            name        = COALESCE(excluded.name, public_channels.name),
            description = COALESCE(excluded.description, public_channels.description),
            subscribers = COALESCE(excluded.subscribers, public_channels.subscribers),
            last_seen   = excluded.last_seen
        """,
        (link, info.name, info.description, info.subscribers, seen_at),
    )


# ---------------------------------------------------------------------------
# Ingestion pipeline (shared by `scrape` and `fetch`)
# ---------------------------------------------------------------------------


def _register_forwards(
    fwd_data: list[ForwardInfo],
    post_id: int,
    channel_map: dict[str, ChannelRecord],
) -> None:
    for fwd in fwd_data:
        if fwd.channel_link not in channel_map:
            channel_map[fwd.channel_link] = ChannelRecord(peer=fwd.peer)
        record = channel_map[fwd.channel_link]
        if post_id not in record.post_ids:
            record.post_ids.append(post_id)


def _post_summary(
    msg: Message,
    channel: str,
    comments_count: int,
    forwarder_from_channel: str | None,
) -> dict:
    """Compact per-post record used by the stdout summary."""
    text = msg.text or ""
    reactions, stars = count_reactions(msg)
    return {
        "id": msg.id,
        "link": tme_link(channel, msg.id),
        "date": msg.date.isoformat() if msg.date else None,
        "text": text,
        "views": msg.views,
        "forwards": msg.forwards,
        "reactions": reactions,
        "stars": stars,
        "tags": extract_tags(text),
        "comments_count": comments_count,
        "forwarder_from_channel": forwarder_from_channel,
    }


async def process_post(
    client: TelegramClient,
    channel_entity,
    channel: str,
    group: list[Message],
    conn: sqlite3.Connection,
    channel_map: dict[str, ChannelRecord],
    media_dir: Path,
    scrape_date: str,
    with_comments: bool,
    with_media: bool,
) -> dict:
    group.sort(key=lambda m: m.id)
    parent = next((m for m in group if m.text), group[0])

    attachments: list[tuple[int, str, str | None, str | None]] = []
    for m in group:
        if m.media is None:
            continue
        photo = await download_photo(client, m, media_dir, with_media)
        attachments.append((m.id, tme_link(channel, m.id), media_type(m), photo))

    fwd_data = (
        await get_public_forwards(client, channel_entity, parent.id)
        if parent.forwards
        else []
    )
    _register_forwards(fwd_data, parent.id, channel_map)

    # If this post is itself a forward of another channel, capture the source
    # channel link and make sure that channel exists in `public_channels`.
    fwd_source = await get_forward_source(client, parent)
    forwarder_from_channel: str | None = None
    if fwd_source is not None:
        forwarder_from_channel = fwd_source.channel_link
        if fwd_source.channel_link not in channel_map:
            channel_map[fwd_source.channel_link] = ChannelRecord(peer=fwd_source.peer)

    comments = (
        await get_comments(client, channel_entity, parent) if with_comments else []
    )

    upsert_post(conn, channel, parent, attachments, forwarder_from_channel)
    insert_metrics(conn, parent, scrape_date, len(comments), len(fwd_data))
    if with_comments:
        replace_comments(conn, parent.id, comments)
    upsert_public_shares(conn, parent.id, fwd_data, scrape_date)
    conn.commit()

    return _post_summary(parent, channel, len(comments), forwarder_from_channel)


async def _persist_messages(
    client: TelegramClient,
    channel_entity,
    channel: str,
    raw: list[Message],
    conn: sqlite3.Connection,
    media_dir: Path,
    scrape_date: str,
    with_comments: bool,
    with_media: bool,
    with_channel_info: bool,
) -> tuple[list[dict], list[dict]]:
    """Group, persist and summarize a batch of fetched messages."""
    post_groups = group_albums(raw)

    post_summaries: list[dict] = []
    channel_map: dict[str, ChannelRecord] = {}
    total = len(post_groups)
    done = 0
    all_ids = [m.id for m in raw]
    id_range = f"{min(all_ids)}..{max(all_ids)}" if all_ids else "—"

    for group in post_groups:
        summary = await process_post(
            client, channel_entity, channel, group, conn, channel_map,
            media_dir, scrape_date, with_comments, with_media,
        )
        post_summaries.append(summary)
        done += 1
        label = (
            f"msg {group[0].id}"
            if len(group) == 1
            else f"group {sorted(m.id for m in group)}"
        )
        _log_progress(done, total, label, id_range)

    log.debug("resolving %d forwarding channels", len(channel_map))
    channel_summaries: list[dict] = []
    for ch_link, record in channel_map.items():
        info = (
            await get_channel_info(client, record.peer)
            if with_channel_info
            else ChannelInfo(name=None, description=None, subscribers=None)
        )
        upsert_public_channel(conn, ch_link, info, scrape_date)
        channel_summaries.append(
            {
                "link": ch_link,
                "name": info.name,
                "subscribers": info.subscribers,
                "shared_posts": sorted(record.post_ids),
            }
        )
    conn.commit()
    channel_summaries.sort(key=lambda c: c["link"])
    post_summaries.sort(key=lambda p: p["id"])
    return post_summaries, channel_summaries


async def ingest(
    channel: str,
    output_dir: Path,
    session_file: str,
    source,
    with_comments: bool,
    with_media: bool,
    with_channel_info: bool,
) -> None:
    """Shared run lifecycle behind `scrape` and `fetch`.

    Opens the DB, connects, pulls messages via `source(client, entity)`,
    persists them, and prints the summary. The two commands differ only in
    their message-source adapter."""
    scrape_date = datetime.now(UTC).isoformat()
    media_dir = output_dir / "media"

    conn = open_db(output_dir, channel)
    try:
        async with channel_session(session_file, channel) as (client, entity):
            raw = await source(client, entity)
            post_summaries, channel_summaries = await _persist_messages(
                client, entity, channel, raw, conn, media_dir,
                scrape_date, with_comments, with_media, with_channel_info,
            )
    finally:
        conn.close()

    summarize_scrape(channel, post_summaries, channel_summaries)


async def scrape(
    channel: str,
    output_dir: Path,
    session_file: str,
    limit: int | None = None,
    offset_id: int = 0,
    offset_date: datetime | None = None,
    latest: int | None = None,
    with_comments: bool = True,
    with_media: bool = True,
    with_channel_info: bool = True,
) -> None:
    # `--latest N` flips iteration to newest-first to actually return the
    # most recent N posts. `--limit N` alone keeps the chronological
    # (oldest-first) walk, which is what you want when paging forward from
    # an offset.
    if latest is not None:
        reverse = False
        iter_limit = latest
        iter_offset_id = 0
        iter_offset_date = None
    else:
        reverse = True
        iter_limit = limit
        # Telethon's offset_id is exclusive; -1 makes --offset-id inclusive.
        iter_offset_id = offset_id - 1 if offset_id else 0
        iter_offset_date = offset_date

    async def source(client: TelegramClient, entity) -> list[Message]:
        log.info("authenticated, scraping %s", channel)
        raw = [
            msg
            async for msg in client.iter_messages(
                channel,
                limit=iter_limit,
                reverse=reverse,
                offset_id=iter_offset_id,
                offset_date=iter_offset_date,
            )
        ]
        log.info("fetched %d messages", len(raw))
        return raw

    await ingest(
        channel, output_dir, session_file, source,
        with_comments, with_media, with_channel_info,
    )


async def fetch_by_ids(
    channel: str,
    post_ids: list[int],
    output_dir: Path,
    session_file: str,
    with_comments: bool = True,
    with_media: bool = True,
    with_channel_info: bool = True,
) -> None:
    async def source(client: TelegramClient, entity) -> list[Message]:
        log.info("authenticated, fetching %d post(s) from %s", len(post_ids), channel)
        # get_messages returns a parallel list; entries are None for missing ids.
        fetched = await client.get_messages(entity, ids=post_ids)
        raw: list[Message] = []
        missing: list[int] = []
        for req_id, msg in zip(post_ids, fetched):
            if isinstance(msg, Message):
                raw.append(msg)
            else:
                missing.append(req_id)
        if missing:
            log.warning("not found in channel: %s", missing)
        log.info("resolved %d/%d post(s)", len(raw), len(post_ids))
        return raw

    await ingest(
        channel, output_dir, session_file, source,
        with_comments, with_media, with_channel_info,
    )


# ---------------------------------------------------------------------------
# Stats: subscribers + views
# ---------------------------------------------------------------------------


async def load_graph(client: TelegramClient, graph) -> dict | None:
    """Resolve a StatsGraph / StatsGraphAsync into its decoded JSON payload."""
    if isinstance(graph, StatsGraphAsync):
        try:
            graph = await client(LoadAsyncGraphRequest(token=graph.token))
        except Exception as e:
            log.error("failed to load async graph (%s)", e)
            return None
    if isinstance(graph, StatsGraph):
        return json.loads(graph.json.data)
    return None


def graph_series(graph: dict) -> tuple[list, dict[str, list]]:
    """Split a decoded stats graph into (x_values, {series_label: values})."""
    x: list = []
    series: dict[str, list] = {}
    names = graph.get("names", {})
    for col in graph["columns"]:
        key, values = col[0], col[1:]
        if key == "x":
            x = values
        else:
            series[names.get(key, key)] = values
    return x, series


def match_series(series: dict[str, list], *keywords: str) -> list | None:
    """Find the series whose label contains any of the keywords."""
    for label, values in series.items():
        if any(k in label.lower() for k in keywords):
            return values
    return None


@asynccontextmanager
async def stats_session(channel: str, session_file: str):
    """Connected client + the channel's BroadcastStats, lifecycle owned here.

    Replaces the old `open_stats`, which handed a live client across the seam
    for the caller to remember to disconnect."""
    async with channel_session(session_file, channel) as (client, entity):
        log.info("authenticated, fetching stats for %s", channel)
        try:
            stats = await client(GetBroadcastStatsRequest(channel=entity))
        except Exception as e:
            log.error(
                "failed to get stats (%s) - you must be an admin of a channel "
                "that is large enough for Telegram to compute statistics",
                e,
            )
            raise typer.Exit(code=1)
        yield client, stats


def ms_to_date(ts_ms) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, UTC).date().isoformat()


async def fetch_subscribers(
    channel: str, output_dir: Path, session_file: str
) -> None:
    async with stats_session(channel, session_file) as (client, stats):
        followers = await load_graph(client, stats.followers_graph)
        growth = await load_graph(client, stats.growth_graph)
        sources = await load_graph(client, stats.new_followers_by_source_graph)

    if not followers:
        log.error("no followers graph available for this channel")
        raise typer.Exit(code=1)

    x, series = graph_series(followers)
    joined = match_series(series, "join") or [None] * len(x)
    left = match_series(series, "left", "leav", "unsub") or [None] * len(x)

    totals: dict = {}
    if growth:
        gx, gseries = graph_series(growth)
        total_values = next(iter(gseries.values()), [])
        totals = dict(zip(gx, total_values))

    # New followers per source: source_label -> {date -> value}.
    source_data: dict[str, dict[str, object]] = {}
    if sources:
        sx, sseries = graph_series(sources)
        for label, values in sseries.items():
            source_data[label] = {ms_to_date(ts): v for ts, v in zip(sx, values)}

    base_rows: list[tuple] = []
    for i, ts_ms in enumerate(x):
        date = ms_to_date(ts_ms)
        leave = left[i]
        # Telegram reports "left" as a negative delta; emit a positive count.
        if isinstance(leave, (int, float)):
            leave = abs(leave)
        total = totals.get(ts_ms)
        base_rows.append((date, total, joined[i], leave))

    source_rows: list[tuple] = []
    for label, by_date in source_data.items():
        for date, value in by_date.items():
            if value in (None, ""):
                continue
            source_rows.append((date, label, value))

    with closing(open_db(output_dir, channel)) as conn:
        conn.executemany(
            """
            INSERT INTO subscribers (date, total, joins, leaves)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                total  = COALESCE(excluded.total,  subscribers.total),
                joins  = COALESCE(excluded.joins,  subscribers.joins),
                leaves = COALESCE(excluded.leaves, subscribers.leaves)
            """,
            base_rows,
        )
        conn.executemany(
            """
            INSERT INTO subscriber_sources (date, source, joins)
            VALUES (?, ?, ?)
            ON CONFLICT(date, source) DO UPDATE SET
                joins = excluded.joins
            """,
            source_rows,
        )
        conn.commit()
        rows = _load_subscriber_rows(conn)

    log.info(
        "stored %d daily rows, %d source rows in %s",
        len(base_rows),
        len(source_rows),
        db_path_for(output_dir, channel),
    )

    summarize_subscribers(channel, rows)


def _load_subscriber_rows(conn: sqlite3.Connection) -> dict[str, dict]:
    """Reconstruct {date: row} where row carries base fields + 'sources' dict."""
    rows: dict[str, dict] = {}
    for date, total, joins, leaves in conn.execute(
        "SELECT date, total, joins, leaves FROM subscribers"
    ):
        rows[date] = {
            "date": date,
            "total": total,
            "joins": joins,
            "leaves": leaves,
            "sources": {},
        }
    for date, source, joins in conn.execute(
        "SELECT date, source, joins FROM subscriber_sources"
    ):
        if date in rows:
            rows[date]["sources"][source] = joins
    return rows


async def fetch_views_by_hour(channel: str, session_file: str) -> None:
    async with stats_session(channel, session_file) as (client, stats):
        graph = await load_graph(client, stats.top_hours_graph)
        period = stats.period

    if not graph:
        log.error("no top-hours graph available for this channel")
        raise typer.Exit(code=1)

    hours, series = graph_series(graph)
    views = next(iter(series.values()), [])
    summarize_views(
        channel,
        hours,
        views,
        period.min_date.date().isoformat(),
        period.max_date.date().isoformat(),
    )


# ---------------------------------------------------------------------------
# Scheduled posts
# ---------------------------------------------------------------------------


async def fetch_scheduled(channel: str, session_file: str) -> None:
    """List the channel's scheduled (not-yet-published) posts.

    Calls messages.GetScheduledHistory directly rather than
    `iter_messages(..., scheduled=True)`: the iterator assumes the normal
    newest-first (descending-id) order and stops after the first message once
    ids start increasing, but scheduled history comes back oldest-first, so the
    iterator only ever yields one post. The raw request returns the whole queue
    in one round-trip. It only returns rows to an account with post rights on
    the channel. Scheduled posts carry no views/forwards/reactions and their
    ids are *scheduled-message* ids (distinct from the id a post gets once
    published), so we don't persist them — this is a read-only peek."""
    async with channel_session(session_file, channel) as (client, entity):
        log.info("authenticated, listing scheduled posts for %s", channel)
        try:
            result = await client(
                GetScheduledHistoryRequest(peer=entity, hash=0)
            )
        except Exception as e:
            log.error(
                "failed to list scheduled posts (%s) - you need post rights on "
                "the channel to see its scheduled queue",
                e,
            )
            raise typer.Exit(code=1)
        raw: list[Message] = [
            m for m in getattr(result, "messages", []) if isinstance(m, Message)
        ]

    items: list[dict] = []
    for group in group_albums(raw):
        group.sort(key=lambda m: m.id)
        # Raw messages from GetScheduledHistory aren't client-bound, so the
        # `.text` property is None; the plain body lives in `.message`.
        parent = next((m for m in group if m.message), group[0])
        attachments = [d for m in group if m.media is not None if (d := _media_desc(m))]
        text = parent.message or ""
        items.append(
            {
                "id": parent.id,
                "date": parent.date.isoformat() if parent.date else None,
                "text": text,
                "attachments": attachments,
            }
        )
    items.sort(key=lambda i: (i["date"] or "", i["id"]))

    summarize_scheduled(channel, items)


def _media_desc(msg: Message) -> str | None:
    """Human-readable one-liner for a scheduled post's attachment."""
    mt = media_type(msg)
    if mt is None:
        return None
    if isinstance(msg.media, MessageMediaDocument):
        doc = msg.media.document
        name = next(
            (
                fn
                for attr in getattr(doc, "attributes", [])
                if (fn := getattr(attr, "file_name", None))
            ),
            None,
        )
        size = getattr(doc, "size", None)
        mime = getattr(doc, "mime_type", None)
        parts = [name or mime or "document"]
        if size:
            parts.append(f"({size:,} bytes)")
        return " ".join(parts)
    return mt


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


app = typer.Typer(help="Scrape posts, forwards and comments from a Telegram channel.")

# Shared option declarations — one home per flag's help text. Commands reuse
# these aliases, so the CLI surface stays identical across commands by
# construction instead of by copy-paste discipline.
ChannelOpt = Annotated[
    str, typer.Option(help="Telegram channel username (required).")
]
AdminChannelOpt = Annotated[
    str,
    typer.Option(help="Telegram channel username, required (you must be an admin)."),
]
PostRightsChannelOpt = Annotated[
    str,
    typer.Option(help="Telegram channel username, required (you need post rights)."),
]
OutputDirOpt = Annotated[
    Path, typer.Option(help="Directory for the SQLite DB and downloaded media.")
]
SessionOpt = Annotated[str, typer.Option(help="Telethon session file name.")]
CommentsOpt = Annotated[bool, typer.Option(help="Fetch post comments.")]
MediaOpt = Annotated[bool, typer.Option(help="Download post media.")]
ChannelInfoOpt = Annotated[
    bool,
    typer.Option(
        help="Resolve detail info about outer public channels that forwarded posts."
    ),
]
VerboseOpt = Annotated[
    bool,
    typer.Option(
        "--verbose", "-v",
        help="Per-post progress + Telethon network logs (otherwise every "
        f"{PROGRESS_EVERY} posts).",
    ),
]
DEFAULT_SESSION = str(DEFAULT_SESSION_FILE)


def _prepare(session_file: str, verbose: bool = False) -> None:
    """Shared command preamble: a session must exist; -v raises log verbosity."""
    _require_session(session_file)
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("telethon").setLevel(logging.INFO)


@app.command("scrape")
def scrape_cmd(
    channel: ChannelOpt,
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
            help="Start at this post id (inclusive) and walk forward to newer "
            "posts. 0 = walk from the beginning of history."
        ),
    ] = 0,
    offset_date: Annotated[
        datetime | None,
        typer.Option(
            formats=["%d-%m-%Y", "%d-%m-%Y %H:%M:%S"],
            help="Start after this date and walk forward to newer posts.",
        ),
    ] = None,
    latest: Annotated[
        int | None,
        typer.Option(
            help="Fetch the N most recent posts (newest-first). Overrides "
            "--limit/--offset-id/--offset-date."
        ),
    ] = None,
    comments: CommentsOpt = True,
    media: MediaOpt = True,
    channel_info: ChannelInfoOpt = True,
    verbose: VerboseOpt = False,
) -> None:
    """Run the scraper."""
    _prepare(session_file, verbose)
    asyncio.run(
        scrape(
            channel,
            output_dir,
            session_file,
            limit,
            offset_id,
            offset_date,
            latest,
            comments,
            media,
            channel_info,
        )
    )


@app.command("fetch")
def fetch_cmd(
    post_ids: Annotated[
        list[int],
        typer.Argument(help="One or more post ids, e.g. `fetch 103 105 108`."),
    ],
    channel: ChannelOpt,
    output_dir: OutputDirOpt = DEFAULT_OUTPUT_DIR,
    session_file: SessionOpt = DEFAULT_SESSION,
    comments: CommentsOpt = True,
    media: MediaOpt = True,
    channel_info: ChannelInfoOpt = True,
    verbose: VerboseOpt = False,
) -> None:
    """Fetch specific posts by id and persist them like `scrape` does.

    Useful for refreshing a known post or pulling a small set without
    iterating the whole channel history. Missing ids are logged and skipped."""
    _prepare(session_file, verbose)
    asyncio.run(
        fetch_by_ids(
            channel,
            post_ids,
            output_dir,
            session_file,
            comments,
            media,
            channel_info,
        )
    )


@app.command("subscribers")
def subscribers(
    channel: AdminChannelOpt,
    output_dir: OutputDirOpt = DEFAULT_OUTPUT_DIR,
    session_file: SessionOpt = DEFAULT_SESSION,
) -> None:
    """Export daily subscriber dynamics into the SQLite DB
    (subscribers + subscriber_sources tables)."""
    _prepare(session_file)
    asyncio.run(fetch_subscribers(channel, output_dir, session_file))


@app.command("views")
def views(
    channel: AdminChannelOpt,
    session_file: SessionOpt = DEFAULT_SESSION,
) -> None:
    """Print views per hour of day to the console: hour|views."""
    _prepare(session_file)
    asyncio.run(fetch_views_by_hour(channel, session_file))


@app.command("scheduled")
def scheduled(
    channel: PostRightsChannelOpt,
    session_file: SessionOpt = DEFAULT_SESSION,
) -> None:
    """List the channel's scheduled (not-yet-published) posts to the console.

    Read-only — scheduled posts have no engagement yet and their ids differ
    from published ids, so nothing is persisted. Requires post rights on the
    channel."""
    _prepare(session_file)
    asyncio.run(fetch_scheduled(channel, session_file))


@app.command("login")
def login(
    session_file: SessionOpt = DEFAULT_SESSION,
) -> None:
    """One-time interactive Telegram auth.

    Run this **in your own terminal** (not via Claude Code's Bash tool) before
    using scrape/fetch/subscribers/views. Telethon prompts on stdin for the
    SMS code (and the 2FA password if you have one enabled), then writes the
    session file. Subsequent commands reuse it."""
    Path(session_file).parent.mkdir(parents=True, exist_ok=True)

    async def _go() -> None:
        async with channel_session(session_file):
            pass

    asyncio.run(_go())
    typer.echo(f"Saved Telegram session to {session_file}")


if __name__ == "__main__":
    app()
