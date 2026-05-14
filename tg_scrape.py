import asyncio
import csv
from collections import Counter
from datetime import datetime, UTC
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.stats import (
    GetBroadcastStatsRequest,
    GetMessagePublicForwardsRequest,
    LoadAsyncGraphRequest,
)
from telethon.tl.types import (
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    PublicForwardMessage,
    ReactionPaid,
    StatsGraph,
    StatsGraphAsync,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
PHONE = os.environ["TG_PHONE"]

DEFAULT_CHANNEL = "@fastnewsdev"
DEFAULT_SESSION_FILE = "session.session"
DEFAULT_OUTPUT_DIR = Path("data")


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


def serialize_attach(msg: Message, channel: str, photo_path: str | None) -> dict:
    result = {
        "id": msg.id,
        "link": tme_link(channel, msg.id),
        "media_type": media_type(msg),
    }
    if photo_path:
        result["photo"] = photo_path
    return result


def serialize_message(
    msg: Message,
    channel: str,
    photo_path: str | None = None,
    attachments: list[dict] | None = None,
    public_forwards: list[str] | None = None,
    comments: list[dict] | None = None,
) -> dict:
    text = msg.text or ""
    reactions, stars = count_reactions(msg)
    result = {
        "id": msg.id,
        "link": tme_link(channel, msg.id),
        "date": msg.date.isoformat() if msg.date else None,
        "text": text,
        "views": msg.views,
        "forwards": msg.forwards,
        "reactions": reactions,
    }

    if stars > 0:
        result["stars"] = stars

    if attachments is None:
        result["media_type"] = media_type(msg)
        if photo_path:
            result["photo"] = photo_path

    if tags := extract_tags(text):
        result["tags"] = tags

    if msg.reply_to_msg_id:
        result["reply_to_msg_id"] = msg.reply_to_msg_id

    if msg.edit_date and msg.edit_date != msg.date:
        result["edit_date"] = msg.edit_date.isoformat()

    if attachments is not None:
        result["attachments"] = attachments

    if public_forwards:
        result["public_forwards"] = public_forwards

    if comments:
        result["comments"] = comments

    return result


async def build_post(
    client: TelegramClient,
    channel_entity,
    group: list[Message],
    channel: str,
    channel_map: dict[str, ChannelRecord],
    media_dir: Path,
    with_comments: bool,
    with_media: bool,
) -> dict:
    group.sort(key=lambda m: m.id)
    parent = next((m for m in group if m.text), group[0])

    attachments = [
        serialize_attach(
            m, channel, await download_photo(client, m, media_dir, with_media)
        )
        for m in group
    ]
    fwd_data = (
        await get_public_forwards(client, channel_entity, parent.id)
        if parent.forwards
        else []
    )
    _register_forwards(fwd_data, parent.id, channel_map)
    comments = (
        await get_comments(client, channel_entity, parent) if with_comments else []
    )
    return serialize_message(
        parent,
        channel,
        attachments=attachments,
        public_forwards=[f.msg_link for f in fwd_data],
        comments=comments,
    )


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


async def scrape(
    channel: str,
    output_dir: Path,
    session_file: str,
    limit: int | None = None,
    offset_id: int = 0,
    offset_date: datetime | None = None,
    with_comments: bool = True,
    with_media: bool = True,
    with_channel_info: bool = True,
) -> None:
    output_file = (
        output_dir / f"posts_{datetime.now(UTC).strftime('%Y-%m-%d_%H-%M-%S')}.json"
    )
    media_dir = output_dir / "media"

    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.start(phone=PHONE)

    channel_entity = await client.get_entity(channel)
    log.info("authenticated, scraping %s", channel)

    raw: list[Message] = [
        msg
        async for msg in client.iter_messages(
            channel,
            limit=limit,
            # Iterate oldest -> newest, starting at (and including) offset_id.
            reverse=True,
            # Telethon's offset_id is exclusive; -1 makes --offset-id inclusive.
            offset_id=offset_id - 1 if offset_id else 0,
            # With reverse=True, fetches messages newer than this date.
            offset_date=offset_date,
        )
    ]
    log.info("fetched %d messages", len(raw))

    groups: dict[int, list[Message]] = {}
    standalone: list[Message] = []
    for msg in raw:
        if msg.grouped_id:
            groups.setdefault(msg.grouped_id, []).append(msg)
        else:
            standalone.append(msg)

    posts: list[dict] = []
    channel_map: dict[str, ChannelRecord] = {}
    total = len(standalone) + len(groups)
    done = 0

    for msg in standalone:
        photo = await download_photo(client, msg, media_dir, with_media)
        fwd_data = (
            await get_public_forwards(client, channel_entity, msg.id)
            if msg.forwards
            else []
        )
        _register_forwards(fwd_data, msg.id, channel_map)
        comments = (
            await get_comments(client, channel_entity, msg) if with_comments else []
        )
        posts.append(
            serialize_message(
                msg,
                channel,
                photo,
                public_forwards=[f.msg_link for f in fwd_data],
                comments=comments,
            )
        )
        done += 1
        log.info("[%d/%d] processed msg %d", done, total, msg.id)

    for group in groups.values():
        posts.append(
            await build_post(
                client,
                channel_entity,
                group,
                channel,
                channel_map,
                media_dir,
                with_comments,
                with_media,
            )
        )
        done += 1
        ids = [m.id for m in group]
        log.info("[%d/%d] processed group %s", done, total, ids)

    posts.sort(key=lambda p: p["id"])

    log.info("resolving %d forwarding channels", len(channel_map))
    channels = []
    for ch_link, record in channel_map.items():
        entry = {"link": ch_link}
        if with_channel_info:
            ch_info = await get_channel_info(client, record.peer)
            entry["name"] = ch_info.name
            entry["description"] = ch_info.description
            entry["subscribers"] = ch_info.subscribers
        entry["shared_posts"] = sorted(record.post_ids)
        channels.append(entry)
    channels.sort(key=lambda c: c["link"])

    output = {"posts": posts, "channels": channels}

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    log.info(
        "saved %d posts, %d channels to %s", len(posts), len(channels), output_file
    )

    await client.disconnect()
    log.info("done")

    summarize_scrape(channel, posts, channels, output_file)


def _text_snippet(text: str | None, length: int = 80) -> str:
    return " ".join((text or "").split())[:length]


def summarize_scrape(
    channel: str, posts: list[dict], channels: list[dict], output_file: Path
) -> None:
    """Print an LLM-oriented summary of a scrape run to stdout."""
    print(f"\n# Scrape summary: {channel}\n")
    if not posts:
        print("No posts fetched.")
        return

    dates = sorted(p["date"] for p in posts if p.get("date"))
    n = len(posts)
    views = sum(p.get("views") or 0 for p in posts)
    forwards = sum(p.get("forwards") or 0 for p in posts)
    reactions = sum(p.get("reactions") or 0 for p in posts)
    comments = sum(len(p.get("comments") or []) for p in posts)

    print(f"- Posts: {n}")
    if dates:
        print(f"- Date range: {dates[0][:10]} -> {dates[-1][:10]}")
    print(f"- Total views: {views:,} (avg {views // n:,}/post)")
    print(
        f"- Total forwards: {forwards:,} | reactions: {reactions:,} "
        f"| comments: {comments:,}"
    )
    print(f"- Forwarding channels: {len(channels)}")
    print(f"- Full data (JSON): {output_file}")

    def top(key: str, n: int = 5) -> list[dict]:
        return sorted(posts, key=lambda p: p.get(key) or 0, reverse=True)[:n]

    print("\n## Top posts by views\n")
    for p in top("views"):
        print(
            f"- {p.get('views') or 0:,} views | {p['link']} | "
            f"{_text_snippet(p.get('text'))}"
        )

    print("\n## Top posts by reactions\n")
    for p in top("reactions"):
        print(
            f"- {p.get('reactions') or 0:,} reactions | {p['link']} | "
            f"{_text_snippet(p.get('text'))}"
        )

    tags = Counter(t for p in posts for t in p.get("tags", []))
    if tags:
        print("\n## Top tags\n")
        for tag, count in tags.most_common(10):
            print(f"- #{tag}: {count}")

    if channels:
        print("\n## Top forwarding channels\n")
        ranked = sorted(
            channels, key=lambda c: c.get("subscribers") or 0, reverse=True
        )
        for c in ranked[:10]:
            subs = c.get("subscribers")
            subs_str = f"{subs:,} subs" if subs else "subs n/a"
            name = c.get("name") or c["link"]
            print(f"- {name} ({subs_str}) | shared {len(c['shared_posts'])} post(s)")


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


async def open_stats(channel: str, session_file: str):
    """Connect, resolve the channel and fetch its BroadcastStats."""
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.start(phone=PHONE)

    channel_entity = await client.get_entity(channel)
    log.info("authenticated, fetching stats for %s", channel)

    try:
        stats = await client(GetBroadcastStatsRequest(channel=channel_entity))
    except Exception as e:
        log.error(
            "failed to get stats (%s) - you must be an admin of a channel that is "
            "large enough for Telegram to compute statistics",
            e,
        )
        await client.disconnect()
        raise typer.Exit(code=1)
    return client, stats


SUBSCRIBERS_BASE_COLUMNS = ["date", "total", "total_subscribe", "unsubscribe"]


def ms_to_date(ts_ms) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, UTC).date().isoformat()


async def fetch_subscribers(
    channel: str, output_dir: Path, session_file: str
) -> None:
    output_file = output_dir / "subscribers.csv"

    client, stats = await open_stats(channel, session_file)

    followers = await load_graph(client, stats.followers_graph)
    growth = await load_graph(client, stats.growth_graph)
    sources = await load_graph(client, stats.new_followers_by_source_graph)
    await client.disconnect()

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

    # New followers per source: column name -> {date -> value}.
    source_columns: list[str] = []
    source_data: dict[str, dict[str, object]] = {}
    if sources:
        sx, sseries = graph_series(sources)
        for label, values in sseries.items():
            column = "subscribe_" + label.lower().replace(" ", "_")
            source_columns.append(column)
            source_data[column] = {
                ms_to_date(ts): v for ts, v in zip(sx, values)
            }

    # Merge into any existing file, keyed by date, so repeated runs accumulate.
    rows: dict[str, dict] = {}
    existing_columns: list[str] = []
    if output_file.exists():
        with output_file.open(newline="") as f:
            reader = csv.DictReader(f, delimiter="|")
            existing_columns = list(reader.fieldnames or [])
            for row in reader:
                rows[row["date"]] = dict(row)

    for i, ts_ms in enumerate(x):
        date = ms_to_date(ts_ms)
        unsub = left[i]
        # Telegram reports "left" as a negative delta; emit a positive count.
        if isinstance(unsub, (int, float)):
            unsub = abs(unsub)
        row = rows.get(date, {})
        row.update(
            {
                "date": date,
                "total": totals.get(ts_ms, ""),
                "total_subscribe": joined[i],
                "unsubscribe": unsub,
            }
        )
        for column in source_columns:
            row[column] = source_data[column].get(date, "")
        rows[date] = row

    # Column order: fixed base columns, then any source columns (old + new).
    columns = list(SUBSCRIBERS_BASE_COLUMNS)
    for column in existing_columns + source_columns:
        if column not in columns:
            columns.append(column)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=columns, delimiter="|", restval=""
        )
        writer.writeheader()
        for date in sorted(rows):
            writer.writerow(rows[date])

    log.info("saved %d rows to %s", len(rows), output_file)
    log.info("done")

    summarize_subscribers(channel, rows, columns, output_file)


def _as_number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def summarize_subscribers(
    channel: str, rows: dict[str, dict], columns: list[str], output_file: Path
) -> None:
    """Print an LLM-oriented summary of subscriber dynamics to stdout."""
    print(f"\n# Subscriber summary: {channel}\n")
    dates = sorted(rows)
    if not dates:
        print("No subscriber data.")
        return

    joins = sum(_as_number(rows[d].get("total_subscribe")) for d in dates)
    leaves = sum(_as_number(rows[d].get("unsubscribe")) for d in dates)
    first_total = _as_number(rows[dates[0]].get("total"))
    last_total = _as_number(rows[dates[-1]].get("total"))
    days = len(dates)

    print(f"- Date range: {dates[0]} -> {dates[-1]} ({days} days)")
    print(f"- Current total subscribers: {int(last_total):,}")
    print(
        f"- Net change over period: {int(last_total - first_total):+,} "
        f"(from {int(first_total):,})"
    )
    print(
        f"- Total joins: {int(joins):,} | total leaves: {int(leaves):,} "
        f"| net: {int(joins - leaves):+,}"
    )
    print(
        f"- Avg per day: {joins / days:.1f} joins, {leaves / days:.1f} leaves"
    )

    best = max(dates, key=lambda d: _as_number(rows[d].get("total_subscribe")))
    worst = max(dates, key=lambda d: _as_number(rows[d].get("unsubscribe")))
    print(
        f"- Best day: {best} "
        f"(+{int(_as_number(rows[best].get('total_subscribe')))} joins)"
    )
    print(
        f"- Worst day: {worst} "
        f"(-{int(_as_number(rows[worst].get('unsubscribe')))} leaves)"
    )
    print(f"- Full data (CSV): {output_file}")

    source_columns = [c for c in columns if c.startswith("subscribe_")]
    if source_columns:
        print("\n## New subscribers by source (period total)\n")
        source_totals = {
            c: sum(_as_number(rows[d].get(c)) for d in dates)
            for c in source_columns
        }
        grand = sum(source_totals.values()) or 1
        for column, value in sorted(
            source_totals.items(), key=lambda kv: kv[1], reverse=True
        ):
            label = column.removeprefix("subscribe_").replace("_", " ")
            print(f"- {label}: {int(value):,} ({value / grand * 100:.1f}%)")


async def fetch_views_by_hour(channel: str, session_file: str) -> None:
    client, stats = await open_stats(channel, session_file)

    graph = await load_graph(client, stats.top_hours_graph)
    period = stats.period
    await client.disconnect()

    if not graph:
        log.error("no top-hours graph available for this channel")
        raise typer.Exit(code=1)

    hours, series = graph_series(graph)
    views = next(iter(series.values()), [])
    summarize_views(channel, hours, views, period)


def summarize_views(channel: str, hours: list, views: list, period) -> None:
    """Print an LLM-oriented summary of views-per-hour to stdout."""
    print(f"\n# Views by hour of day: {channel}\n")
    pairs = [(int(h), _as_number(v)) for h, v in zip(hours, views)]
    if not pairs:
        print("No views-by-hour data.")
        return

    total = sum(v for _, v in pairs) or 1
    ranked = sorted(pairs, key=lambda hv: hv[1], reverse=True)

    print(
        f"- Analyzed period: {period.min_date.date().isoformat()} -> "
        f"{period.max_date.date().isoformat()}"
    )
    print(f"- Total views in sample: {int(total):,}")
    print("- Hour is hour-of-day, 0-23 (UTC).")

    print("\n## Peak hours\n")
    for hour, value in ranked[:3]:
        print(f"- {hour:02d}:00 | {int(value):,} views ({value / total * 100:.1f}%)")

    print("\n## Quietest hours\n")
    for hour, value in sorted(ranked[-3:]):
        print(f"- {hour:02d}:00 | {int(value):,} views ({value / total * 100:.1f}%)")

    print("\n## All hours\n")
    for hour, value in sorted(pairs):
        print(f"- {hour:02d}:00 | {int(value):,} views ({value / total * 100:.1f}%)")


app = typer.Typer(help="Scrape posts, forwards and comments from a Telegram channel.")


@app.command("scrape")
def main(
    channel: Annotated[
        str, typer.Option(help="Telegram channel username to scrape.")
    ] = DEFAULT_CHANNEL,
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for the output JSON and downloaded media."),
    ] = DEFAULT_OUTPUT_DIR,
    session_file: Annotated[
        str, typer.Option(help="Telethon session file name.")
    ] = DEFAULT_SESSION_FILE,
    limit: Annotated[
        int | None,
        typer.Option(help="Max number of messages to fetch (all if omitted)."),
    ] = None,
    offset_id: Annotated[
        int,
        typer.Option(
            help="Start from this post id, fetching only older posts (0 = latest)."
        ),
    ] = 0,
    offset_date: Annotated[
        datetime | None,
        typer.Option(
            formats=["%d-%m-%Y", "%d-%m-%Y %H:%M:%S"],
            help="Start from this date, fetching only newer posts.",
        ),
    ] = None,
    comments: Annotated[
        bool,
        typer.Option(help="Fetch post comments."),
    ] = True,
    media: Annotated[
        bool,
        typer.Option(help="Download post media."),
    ] = True,
    channel_info: Annotated[
        bool,
        typer.Option(
            help="Resolve detail info about outer public channels that forwarded posts."
        ),
    ] = True,
) -> None:
    """Run the scraper."""
    asyncio.run(
        scrape(
            channel,
            output_dir,
            session_file,
            limit,
            offset_id,
            offset_date,
            comments,
            media,
            channel_info,
        )
    )


@app.command("subscribers")
def subscribers(
    channel: Annotated[
        str, typer.Option(help="Telegram channel username (you must be an admin).")
    ] = DEFAULT_CHANNEL,
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for the output CSV."),
    ] = DEFAULT_OUTPUT_DIR,
    session_file: Annotated[
        str, typer.Option(help="Telethon session file name.")
    ] = DEFAULT_SESSION_FILE,
) -> None:
    """Export daily subscriber dynamics as CSV: date, total, total_subscribe,
    unsubscribe and per-source subscribe columns."""
    asyncio.run(fetch_subscribers(channel, output_dir, session_file))


@app.command("views")
def views(
    channel: Annotated[
        str, typer.Option(help="Telegram channel username (you must be an admin).")
    ] = DEFAULT_CHANNEL,
    session_file: Annotated[
        str, typer.Option(help="Telethon session file name.")
    ] = DEFAULT_SESSION_FILE,
) -> None:
    """Print views per hour of day to the console: hour|views."""
    asyncio.run(fetch_views_by_hour(channel, session_file))


if __name__ == "__main__":
    app()
