# tg-analytic-skill

A Claude Code skill that scrapes a Telegram channel into a per-channel SQLite
DB and answers analytics questions over it.

## Language

**Channel**:
The Telegram broadcast channel being analyzed (e.g. @fastnewsdev). Posts
originate here; only admins can post.

**Discussion group**:
The supergroup linked to the channel (`linked_chat_id`), where channel posts
auto-forward and comment threads live.
_Avoid_: comments group, chat, attached group

**Standalone group**:
A supergroup analyzed in its own right, not linked to any channel under
analysis. Has join/leave events and engagement but no threads (threads
require an originating channel post).

**Post**:
A message published in the channel. Identified by its channel message id.

**Comment**:
A message in the discussion group replying (directly or transitively) to an auto-forwarded channel post. Stored in `post_comments`.

**Group message**:
Any non-service message in the discussion group, comments included. The
self-contained record for group analytics (`group_messages`); overlaps with
Comment by design — `post_comments` remains the channel-scrape's view.

**Thread**:
The set of group messages replying (directly or transitively) to one
auto-forwarded channel post. Identified by the originating post's id.

**Top-level chatter**:
Group messages outside any thread (no originating post).
_Avoid_: general messages, off-topic

**Join event**:
A dated service message in the discussion group recording a user joining
(by link, by request approval, or added by a member).

**Leave event**:
A dated service message recording a user leaving the discussion group —
self-leave, removed by an admin, or actor unknown (Telegram omits the
actor e.g. when auto-removing deleted accounts).
