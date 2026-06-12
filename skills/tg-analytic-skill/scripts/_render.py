"""Markdown renderers: plain summary dicts in, LLM-oriented stdout out.

Pure presentation — no Telegram, no SQLite, no Telethon types. The dict
shapes tg_scrape.py builds are the interface; anything here is testable by
fabricating those dicts. Every renderer prints a Markdown block designed to
be pasted to the user as-is (see SKILL.md "Reporting back to the user").
"""

from collections import Counter
from datetime import datetime, timezone

# `datetime.UTC` is 3.11+; alias it from `timezone.utc` for 3.10 compatibility.
UTC = timezone.utc


def _text_snippet(text: str | None, length: int = 80) -> str:
    return " ".join((text or "").split())[:length]


def _md_cell(text: str | None) -> str:
    """Snippet safe for a Markdown table cell - escape pipes, drop newlines."""
    return _text_snippet(text).replace("|", "\\|") or "—"


def _as_number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rel_when(iso: str | None, now: datetime) -> str:
    """Coarse, agent-friendly delta from `now`, e.g. 'in ~3h' / 'overdue 10m'."""
    if not iso:
        return "no date"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    secs = (dt - now).total_seconds()
    overdue = secs < 0
    secs = abs(secs)
    if secs < 3600:
        mag = f"{int(secs // 60)}m"
    elif secs < 86400:
        mag = f"{int(secs // 3600)}h"
    else:
        mag = f"{int(secs // 86400)}d"
    return f"overdue {mag}" if overdue else f"in ~{mag}"


def summarize_scrape(channel: str, posts: list[dict], channels: list[dict]) -> None:
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
    comments = sum(p.get("comments_count") or 0 for p in posts)

    # Outward forwarders only (channels that re-shared our posts). Inward
    # sources we forwarded from are registered into the same channel_map for
    # `public_channels` persistence but carry no `shared_posts`, so filter.
    forwarders = [c for c in channels if c.get("shared_posts")]

    print("## Overview\n")
    span = f"  ({dates[0][:10]} → {dates[-1][:10]})" if dates else ""
    print(f"- Posts: {n}{span}")
    print(f"- Views: {views:,}  (avg {views // n:,}/post)")
    print(f"- Reactions: {reactions:,}   Comments: {comments:,}   "
          f"Forwards of your posts: {forwards:,}")
    print(f"- Your posts re-shared by: {len(forwarders)} other channels")

    # One combined ranking, sorted by views, with reactions alongside - half
    # the lines of two separate tables and both signals visible at once.
    top = sorted(posts, key=lambda p: p.get("views") or 0, reverse=True)[:10]
    print("\n## Top posts\n")
    print("| Views | Reactions | Post | Snippet |")
    print("|------:|----------:|------|---------|")
    for p in top:
        print(
            f"| {p.get('views') or 0:,} | {p.get('reactions') or 0:,} "
            f"| {p['link']} | {_md_cell(p.get('text'))} |"
        )

    if forwarders:
        # Group by post (not by channel): for each of our posts that got
        # re-shared, list the channels that shared it. Inverts the
        # forwarder->posts mapping we already have - no extra API calls.
        post_by_id = {p["id"]: p for p in posts}
        shares_by_post: dict[int, list[dict]] = {}
        for c in forwarders:
            for pid in c["shared_posts"]:
                shares_by_post.setdefault(pid, []).append(c)
        total_shares = sum(len(chs) for chs in shares_by_post.values())
        # Direction matters and gets confused easily: this section is OTHERS
        # re-sharing US. The opposite direction (our channel reposting others)
        # is the "YOUR reposts" section below. Spell it out in the headings.
        print(
            f"\n## Who re-shared YOUR posts "
            f"({len(shares_by_post)} of your posts re-shared, "
            f"{total_shares} shares by {len(forwarders)} other channels)\n"
        )
        print(
            "Direction: OTHER channels forwarded YOUR content (your reach). "
            "Each of your posts below was re-shared by the listed channels. "
            "`subs` is each channel's size (the audience that share reached).\n"
        )
        for pid in sorted(shares_by_post, reverse=True):
            chans = sorted(
                shares_by_post[pid],
                key=lambda c: c.get("subscribers") or 0,
                reverse=True,
            )
            p = post_by_id.get(pid)
            if p is not None:
                views = p.get("views")
                views_str = f"{views:,} views" if views is not None else "views n/a"
                print(f"### #{pid} ({views_str}) — {p['link']}")
                snippet = _text_snippet(p.get("text"))
                if snippet:
                    print(f'"{snippet}"')
            else:
                # Re-shared post is outside this scrape's window; id only.
                print(f"### #{pid}")
            for c in chans:
                subs = c.get("subscribers")
                subs_str = f"{subs:,} subs" if subs is not None else "subs n/a"
                name = c.get("name") or c["link"]
                print(f"- {name} ({subs_str}) — {c['link']}")
            print()

    # Our posts that forward/cite another channel — one row per post, newest
    # first. Source channel name/subs joined from `channels`.
    cited_posts = [p for p in posts if p.get("forwarder_from_channel")]
    if cited_posts:
        by_link = {c["link"]: c for c in channels}
        print("\n## YOUR reposts of OTHER channels (not your original content)\n")
        print(
            "Direction: YOUR channel forwarded SOMEONE ELSE's content — the "
            "opposite of the re-shares section above.\n"
        )
        print("| Post | Snippet | Reposted from |")
        print("|------|---------|---------------|")
        for p in sorted(cited_posts, key=lambda p: p["id"], reverse=True):
            link = p["forwarder_from_channel"]
            info = by_link.get(link, {})
            name = info.get("name") or link
            subs = info.get("subscribers")
            subs_str = f"{subs:,} subs" if subs else "subs n/a"
            print(
                f"| {p['link']} | {_md_cell(p.get('text'))} "
                f"| {name} ({subs_str}) {link} |"
            )


def summarize_subscribers(channel: str, rows: dict[str, dict]) -> None:
    """Print an LLM-oriented summary of subscriber dynamics to stdout."""
    print(f"\n# Subscriber summary: {channel}\n")
    dates = sorted(rows)
    if not dates:
        print("No subscriber data.")
        return

    joins = sum(_as_number(rows[d].get("joins")) for d in dates)
    leaves = sum(_as_number(rows[d].get("leaves")) for d in dates)
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
    print(f"- Avg per day: {joins / days:.1f} joins, {leaves / days:.1f} leaves")

    best = max(dates, key=lambda d: _as_number(rows[d].get("joins")))
    worst = max(dates, key=lambda d: _as_number(rows[d].get("leaves")))
    print(f"- Best day: {best} (+{int(_as_number(rows[best].get('joins')))} joins)")
    print(
        f"- Worst day: {worst} "
        f"(-{int(_as_number(rows[worst].get('leaves')))} leaves)"
    )

    source_totals: Counter = Counter()
    for d in dates:
        for source, count in rows[d].get("sources", {}).items():
            source_totals[source] += _as_number(count)
    if source_totals:
        print("\n## New subscribers by source (period total)\n")
        grand = sum(source_totals.values()) or 1
        for source, value in source_totals.most_common():
            print(f"- {source}: {int(value):,} ({value / grand * 100:.1f}%)")


def summarize_scheduled(channel: str, items: list[dict]) -> None:
    """Print the scheduled-post queue to stdout, one block per post."""
    print(f"\n# Scheduled posts: {channel}\n")
    if not items:
        print("No scheduled posts in the queue.")
        return

    now = datetime.now(UTC)
    dates = [i["date"] for i in items if i.get("date")]
    print("## Overview\n")
    print(f"- Queued posts: {len(items)}")
    if dates:
        lo = dates[0][:16].replace("T", " ")
        hi = dates[-1][:16].replace("T", " ")
        print(f"- Window: {lo} → {hi} UTC")
    print("- Times are UTC. Scheduled posts have no engagement metrics yet.")
    print(
        "- `sched-msg #` is the scheduled-message id, distinct from the id the "
        "post gets once published.\n"
    )

    print("## Queue\n")
    for n, i in enumerate(items, 1):
        when = (i.get("date") or "")[:16].replace("T", " ") or "no date"
        rel = _rel_when(i.get("date"), now)
        print(f"### {n}. {when} UTC ({rel}) — sched-msg #{i['id']}\n")
        body = (i.get("text") or "").strip()
        if body:
            print("Text:")
            for line in body.splitlines():
                print(f"> {line}")
        else:
            print("Text: (none)")
        attachments = i.get("attachments") or []
        if attachments:
            print("\nAttachments:")
            for a in attachments:
                print(f"- {a}")
        else:
            print("\nAttachments: (none)")
        print()


def summarize_views(
    channel: str, hours: list, views: list, period_start: str, period_end: str
) -> None:
    """Print an LLM-oriented summary of views-per-hour to stdout."""
    print(f"\n# Views by hour of day: {channel}\n")
    pairs = [(int(h), _as_number(v)) for h, v in zip(hours, views)]
    if not pairs:
        print("No views-by-hour data.")
        return

    total = sum(v for _, v in pairs) or 1
    ranked = sorted(pairs, key=lambda hv: hv[1], reverse=True)

    print(f"- Analyzed period: {period_start} -> {period_end}")
    print(f"- Total views in sample: {int(total):,}")
    print(
        "- Hour is hour-of-day, 0-23, in the Telegram account's local "
        "timezone (NOT UTC)."
    )

    print("\n## Peak hours\n")
    for hour, value in ranked[:3]:
        print(f"- {hour:02d}:00 | {int(value):,} views ({value / total * 100:.1f}%)")

    print("\n## Quietest hours\n")
    for hour, value in sorted(ranked[-3:]):
        print(f"- {hour:02d}:00 | {int(value):,} views ({value / total * 100:.1f}%)")

    print("\n## All hours\n")
    for hour, value in sorted(pairs):
        print(f"- {hour:02d}:00 | {int(value):,} views ({value / total * 100:.1f}%)")
