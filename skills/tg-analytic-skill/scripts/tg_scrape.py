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
from telethon.tl.functions.channels import (
    GetAdminLogRequest,
    GetFullChannelRequest,
)
from telethon.tl.functions.messages import GetScheduledHistoryRequest
from telethon.tl.functions.stats import (
    GetBroadcastStatsRequest,
    GetMessagePublicForwardsRequest,
    LoadAsyncGraphRequest,
)
from telethon.tl.types import (
    ChannelAdminLogEventsFilter,
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageService,
    PeerChannel,
    PublicForwardMessage,
    ReactionPaid,
    StatsGraph,
    StatsGraphAsync,
)

from utils._common import DATA_DIR, DEFAULT_OUTPUT_DIR, db_path_for, open_db
from utils._tg import DEFAULT_SESSION, _require_session, channel_session
from utils._group import (
    GroupEvent,
    auto_forward_post_id,
    classify_admin_log_event,
    classify_service_message,
    thread_post_id_for,
    unresolved_root_refs,
)
from utils._render import (
    summarize_group,
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
) -> list[Message]:
    """One post's comment thread — group-side Message objects, sorted by id.

    Sender extraction happens later in _sender_fields (shared with the
    group scan), so this stays a plain fetch."""
    if not (msg.replies and msg.replies.replies):
        return []
    comments: list[Message] = []
    try:
        async for c in client.iter_messages(channel_entity, reply_to=msg.id):
            if isinstance(c, Message):
                comments.append(c)
    except Exception as e:
        log.error("msg %d: failed to fetch comments (%s)", msg.id, e)
    comments.sort(key=lambda c: c.id)
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


def replace_thread_comments(
    conn: sqlite3.Connection,
    post_id: int,
    comments: list[Message],
) -> None:
    """Replace one post's comment thread in group_messages.

    The scoped DELETE keeps the old per-post deletion tracking (a comment
    removed on Telegram disappears on re-scrape) without touching thread
    roots or top-level chatter, which the group scan owns."""
    conn.execute(
        "DELETE FROM group_messages"
        " WHERE thread_post_id = ? AND is_thread_root = 0",
        (post_id,),
    )
    upsert_group_messages(
        conn,
        [_message_row(m, thread_post=post_id, is_root=False) for m in comments],
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
    # Without --comments we still know the count from the post itself; writing
    # 0 would poison the post_metrics time-series.
    comments_count = (
        len(comments)
        if with_comments
        else (parent.replies.replies if parent.replies else 0)
    )

    upsert_post(conn, channel, parent, attachments, forwarder_from_channel)
    insert_metrics(conn, parent, scrape_date, comments_count, len(fwd_data))
    if with_comments:
        replace_thread_comments(conn, parent.id, comments)
    upsert_public_shares(conn, parent.id, fwd_data, scrape_date)
    conn.commit()

    return _post_summary(parent, channel, comments_count, forwarder_from_channel)


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
    """(user_id, display name, username) for a message's sender — shared by
    the group scan and the scrape-side comment path."""
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
    # SQLite treats NULLs in a composite PK as distinct, so ON CONFLICT
    # can't dedupe the rare events whose user_id is unknown — pre-delete
    # them for the incoming ids to keep re-scans idempotent.
    conn.executemany(
        "DELETE FROM group_events WHERE id = ? AND user_id IS NULL",
        [(e.id,) for e in events if e.user_id is None],
    )
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


async def _fetch_admin_log_events(
    client: TelegramClient, entity
) -> tuple[list[GroupEvent], dict[int, tuple[str | None, str | None]]]:
    """Joins/leaves from the group's admin log (~48h retention), plus the
    subjects' (name, username) from the log's own user objects.

    The log records membership changes even when Telegram suppresses or
    deletes the corresponding service messages — which is exactly what
    happens to CTA join bursts — so for an admin account it is the
    authoritative join source. Requires admin; degrades to empty with a
    notice otherwise."""
    events_filter = ChannelAdminLogEventsFilter(
        join=True, leave=True, invite=True, ban=True, unban=False,
        kick=True, unkick=False, promote=False, demote=False, info=False,
        settings=False, pinned=False, edit=False, delete=False,
        group_call=False, invites=False, send=False, forums=False,
    )
    events: list[GroupEvent] = []
    users: dict[int, tuple[str | None, str | None]] = {}
    max_id = 0
    try:
        while True:
            res = await client(
                GetAdminLogRequest(
                    channel=entity, q="", max_id=max_id, min_id=0,
                    limit=100, events_filter=events_filter,
                )
            )
            if not res.events:
                break
            for ev in res.events:
                events.extend(classify_admin_log_event(ev))
            for u in res.users:
                first = getattr(u, "first_name", "") or ""
                last = getattr(u, "last_name", "") or ""
                users[u.id] = (
                    (first + " " + last).strip() or None,
                    getattr(u, "username", None),
                )
            max_id = res.events[-1].id
    except Exception as e:
        log.info(
            "admin log unavailable (admin rights needed) - joins/leaves "
            "rely on service messages only (%s)", e,
        )
        return [], {}
    if events:
        log.info(
            "admin log: %d membership event(s) (~48h retention - run "
            "`group` at least every 2 days to keep the series complete)",
            len(events),
        )
    return events, users


def _dedupe_admin_events(
    conn: sqlite3.Connection,
    admin_events: list[GroupEvent],
    service_events: list[GroupEvent],
) -> list[GroupEvent]:
    """Drop admin-log events already captured as service messages.

    The same join can appear in both sources under unrelated ids, so the
    (id, user_id) PK can't dedupe across them — match on (user_id, kind)
    within a 10-minute window instead, against both this run's service
    events and rows from earlier runs."""

    def near(a: str | None, b: str | None) -> bool:
        if not a or not b:
            return False
        try:
            da, db = datetime.fromisoformat(a), datetime.fromisoformat(b)
        except ValueError:
            return False
        return abs((da - db).total_seconds()) <= 600

    kept = []
    for e in admin_events:
        if any(
            s.user_id == e.user_id and s.kind == e.kind and near(s.date, e.date)
            for s in service_events
        ):
            continue
        row = conn.execute(
            """
            SELECT 1 FROM group_events
            WHERE user_id = ? AND kind = ? AND id != ?
              AND abs(strftime('%s', datetime(date))
                      - strftime('%s', datetime(?))) <= 600
            LIMIT 1
            """,
            (e.user_id, e.kind, e.id, e.date),
        ).fetchone()
        if row is None:
            kept.append(e)
    dropped = len(admin_events) - len(kept)
    if dropped:
        log.debug("admin log: %d duplicate event(s) skipped", dropped)
    return kept


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


def _message_row(
    msg: Message, thread_post: int | None, is_root: bool
) -> tuple:
    """group_messages row tuple — shared by the group scan and the
    scrape-side comment path (which knows its thread_post directly)."""
    uid, name, username = _sender_fields(msg)
    reactions, stars = count_reactions(msg)
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


def _group_message_row(msg: Message, root_map: dict[int, int]) -> tuple:
    is_root = msg.id in root_map
    thread_post = (
        root_map[msg.id] if is_root else thread_post_id_for(msg, root_map)
    )
    return _message_row(msg, thread_post, is_root)


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
                target.title or target.link, handle,
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
            # Snapshot the window NOW: back-fetched out-of-window roots get
            # appended to `ordinary` below, and their (much older) ids would
            # otherwise drag `lo` down — making the thread-stats query and
            # the reported id_range cover prior scans' rows, not this one's.
            scanned_ids = [m.id for m in raw]

            events = [e for m in service for e in classify_service_message(m)]
            admin_events, users = await _fetch_admin_log_events(
                client, target.entity
            )
            if admin_events:
                events.extend(
                    _dedupe_admin_events(conn, admin_events, events)
                )
            # Admin-log responses carry the subjects' user objects; only
            # resolve the (rare) uids the log didn't cover.
            unresolved = [
                e for e in events
                if e.user_id is not None and e.user_id not in users
            ]
            users.update(await _resolve_event_users(client, unresolved))

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
                for m in fetched:
                    if not isinstance(m, Message):
                        continue
                    pid = auto_forward_post_id(m, target.channel_id)
                    if pid:
                        root_map[m.id] = pid
                        ordinary.append(m)

            rows = [_group_message_row(m, root_map) for m in ordinary]
            upsert_group_messages(conn, rows)
            upsert_group_events(conn, events, users)
            insert_group_metrics(conn, scrape_date, target)
            conn.commit()
            log.info(
                "stored %d message(s), %d event(s) in %s",
                len(rows), len(events), db_path_for(output_dir, handle),
            )

        lo, hi = (
            (min(scanned_ids), max(scanned_ids)) if scanned_ids else (0, 0)
        )
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
        "id_range": f"{lo}..{hi}" if scanned_ids else "—",
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
    summarize_group(handle, overview, messages, event_dicts, threads)


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
