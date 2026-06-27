# tg-analytic-skill

A Claude Code skill that scrapes a Telegram channel into a per-channel SQLite
DB and answers analytics questions over it. It can also queue a future post to
the channel (the one write capability; everything else only reads).

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

**Scheduled post**:
A post queued for Telegram to publish at a future instant, not yet live. Its
id is a scheduled-message id distinct from the published-post id it later
gets, carries no engagement, and is not persisted in the DB. May only be
queued at least one hour ahead (the minimum lead time).
_Avoid_: draft, pending post

**Comment**:
A message in the discussion group replying (directly or transitively) to an auto-forwarded channel post. Stored in `group_messages` with `thread_post_id` set to the originating post's id.

**Group message**:
Any non-service message in the discussion group, comments included. The
single self-contained record for group analytics and comments
(`group_messages`), written by both the channel scrape and the group scan.

**Thread**:
The set of group messages replying (directly or transitively) to one
auto-forwarded channel post. Identified by the originating post's id.

**Top-level chatter**:
Group messages outside any thread (no originating post).
_Avoid_: general messages, off-topic

**Join event**:
A dated record of a user joining the discussion group (by link, by request
approval, or added by a member / Join button). Sourced from service
messages, or from the group's admin log when scanning as an admin —
Telegram suppresses service messages during join bursts.

**Leave event**:
A dated record of a user leaving the discussion group — self-leave,
removed by an admin, or actor unknown (Telegram omits the actor e.g. when
auto-removing deleted accounts). Same two sources as Join event.
