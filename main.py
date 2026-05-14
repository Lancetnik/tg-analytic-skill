import asyncio
from datetime import datetime, UTC
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.stats import GetMessagePublicForwardsRequest
from telethon.tl.types import (
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    PublicForwardMessage,
    ReactionPaid,
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

CHANNEL = "@fastnewsdev"
SESSION_FILE = "session"

ROOT_DIR = Path("/Users/nikitapastukhov/Desktop/Documents/tg-channel")

OUTPUT_FILE = (
    ROOT_DIR / f"posts_{datetime.now(UTC).strftime('%Y-%m-%d_%H-%M-%S')}.json"
)
MEDIA_DIR = ROOT_DIR / "media"


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


async def download_photo(client: TelegramClient, msg: Message) -> str | None:
    if not isinstance(msg.media, MessageMediaPhoto):
        return None
    MEDIA_DIR.mkdir(exist_ok=True)
    dest = MEDIA_DIR / f"{msg.id}.jpg"
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
) -> dict:
    group.sort(key=lambda m: m.id)
    parent = next((m for m in group if m.text), group[0])

    attachments = [
        serialize_attach(m, channel, await download_photo(client, m)) for m in group
    ]
    fwd_data = (
        await get_public_forwards(client, channel_entity, parent.id)
        if parent.forwards
        else []
    )
    _register_forwards(fwd_data, parent.id, channel_map)
    comments = await get_comments(client, channel_entity, parent)
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


async def scrape(limit: int | None = None) -> None:
    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await client.start(phone=PHONE)

    channel_entity = await client.get_entity(CHANNEL)
    log.info("authenticated, scraping %s", CHANNEL)

    raw: list[Message] = [
        msg
        async for msg in client.iter_messages(CHANNEL, limit=limit)
        if isinstance(msg, Message)
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
        photo = await download_photo(client, msg)
        fwd_data = (
            await get_public_forwards(client, channel_entity, msg.id)
            if msg.forwards
            else []
        )
        _register_forwards(fwd_data, msg.id, channel_map)
        comments = await get_comments(client, channel_entity, msg)
        posts.append(
            serialize_message(
                msg,
                CHANNEL,
                photo,
                public_forwards=[f.msg_link for f in fwd_data],
                comments=comments,
            )
        )
        done += 1
        log.info("[%d/%d] processed msg %d", done, total, msg.id)

    for group in groups.values():
        posts.append(
            await build_post(client, channel_entity, group, CHANNEL, channel_map)
        )
        done += 1
        ids = [m.id for m in group]
        log.info("[%d/%d] processed group %s", done, total, ids)

    posts.sort(key=lambda p: p["id"])

    log.info("resolving %d forwarding channels", len(channel_map))
    channels = []
    for ch_link, record in channel_map.items():
        ch_info = await get_channel_info(client, record.peer)
        channels.append(
            {
                "link": ch_link,
                "name": ch_info.name,
                "description": ch_info.description,
                "subscribers": ch_info.subscribers,
                "shared_posts": sorted(record.post_ids),
            }
        )
    channels.sort(key=lambda c: c["link"])

    output = {"posts": posts, "channels": channels}

    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    log.info(
        "saved %d posts, %d channels to %s", len(posts), len(channels), OUTPUT_FILE
    )

    await client.disconnect()
    log.info("done")


if __name__ == "__main__":
    asyncio.run(scrape())
