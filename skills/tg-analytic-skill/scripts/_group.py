"""Discussion-group scan logic: service-message classification and thread
linkage. Stdlib-only and duck-typed over Telethon objects — dispatch is on
`type(action).__name__`, attribute access via getattr — so tg_query.py's
empty-deps sibling property holds and the module stays importable without
Telethon installed.
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
        # `uid is not None` guards the degenerate case where both ids are
        # missing — None == None would misclassify an unknown actor as a
        # self-leave.
        via = "self" if uid is not None and uid == sender else "removed"
        return [GroupEvent(msg.id, date, "leave", via, uid)]
    return []


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
    top = getattr(reply, "reply_to_top_id", None)
    return top if top is not None else getattr(reply, "reply_to_msg_id", None)


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
