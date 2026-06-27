"""Markdown -> (plain text, Telethon MessageEntity list), no HTML round-trip.

Replaces the earlier mistune->HTML->sulguk->shim chain (see docs/adr/0003):
Telegram messages are always plain text plus `MessageEntity` offsets, so we
walk mistune's AST and emit those entities directly. One dependency (mistune),
full control over UTF-16 offsets, and tables become aligned monospace instead
of crashing (Telegram has no table entity).

Supported markup:
  **bold**  *italic*/_italic_  ~~strike~~  ^^underline^^  ||spoiler||
  `code`  ```fenced```  [text](url)  > quote  - / 1. lists  # headings
  | md | tables | -> monospace `pre` block.  #hashtags and emoji pass through.

Offsets are UTF-16 code units (Telegram's convention) — an astral emoji like
🚀 counts as 2, which `_u16len` accounts for.
"""
import mistune
from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityItalic,
    MessageEntityPre,
    MessageEntitySpoiler,
    MessageEntityStrike,
    MessageEntityTextUrl,
    MessageEntityUnderline,
)


def _u16len(s: str) -> int:
    """Length of `s` in UTF-16 code units — the unit Telegram entity offsets use."""
    return len(s.encode("utf-16-le")) // 2


# --- custom inline spoiler: ||hidden|| (mistune ships only block `>!` spoiler) ---
def _parse_spoiler(inline, m, state):
    pos = m.end()
    end = state.src.find("||", pos)
    if end == -1:
        return None
    inner = state.copy()
    inner.src = state.src[pos:end]
    state.append_token({"type": "spoiler", "children": inline.render(inner)})
    return end + 2


def _spoiler_plugin(md) -> None:
    md.inline.register("spoiler", r"\|\|(?=[^\s|])", _parse_spoiler, before="link")


# AST mode (renderer=None): `_MD(src)` returns the token tree, not HTML.
_MD = mistune.create_markdown(
    renderer=None,
    plugins=["strikethrough", "insert", "table", "url", _spoiler_plugin],
)


class _Builder:
    """Accumulates plain text and entities, tracking the UTF-16 cursor."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.len16 = 0
        self.entities: list = []

    def text(self, s: str) -> None:
        if s:
            self.parts.append(s)
            self.len16 += _u16len(s)

    def mark(self) -> int:
        return self.len16

    def span(self, factory, start: int) -> None:
        """Emit an entity covering [start, cursor) if non-empty."""
        length = self.len16 - start
        if length > 0:
            self.entities.append(factory(start, length))

    def result(self) -> tuple[str, list]:
        # Telegram is tolerant of entity order, but sort for determinism.
        self.entities.sort(key=lambda e: (e.offset, -e.length))
        return "".join(self.parts), self.entities


_SPAN = {
    "strong": MessageEntityBold,
    "emphasis": MessageEntityItalic,
    "strikethrough": MessageEntityStrike,
    "insert": MessageEntityUnderline,
    "spoiler": MessageEntitySpoiler,
}


def _inline(b: _Builder, tokens: list) -> None:
    for tok in tokens:
        t = tok["type"]
        if t == "text":
            b.text(tok["raw"])
        elif t in _SPAN:
            start = b.mark()
            _inline(b, tok["children"])
            b.span(_SPAN[t], start)
        elif t == "codespan":
            start = b.mark()
            b.text(tok["raw"])
            b.span(MessageEntityCode, start)
        elif t == "link":
            url = tok["attrs"]["url"]
            start = b.mark()
            _inline(b, tok.get("children") or [{"type": "text", "raw": url}])
            b.span(lambda o, ln, u=url: MessageEntityTextUrl(o, ln, u), start)
        elif t in ("softbreak", "linebreak"):
            b.text("\n")
        elif "children" in tok:
            _inline(b, tok["children"])
        elif "raw" in tok:
            # inline_html or any other leaf — keep its text literally.
            b.text(tok["raw"])


def _cell_text(cell: dict) -> str:
    """Flatten a table cell to plain text (formatting inside cells is dropped)."""
    sub = _Builder()
    _inline(sub, cell.get("children", []))
    return "".join(sub.parts).replace("\n", " ")


def _render_table(b: _Builder, tok: dict) -> None:
    head: list[str] = []
    rows: list[list[str]] = []
    for section in tok["children"]:
        if section["type"] == "table_head":
            head = [_cell_text(c) for c in section["children"]]
        elif section["type"] == "table_body":
            rows = [
                [_cell_text(c) for c in row["children"]]
                for row in section["children"]
            ]
    cols = max([len(head)] + [len(r) for r in rows], default=0)
    widths = [0] * cols
    for cells in [head, *rows]:
        for i in range(cols):
            widths[i] = max(widths[i], len(cells[i]) if i < len(cells) else 0)

    def fmt(cells: list[str]) -> str:
        return " | ".join(
            (cells[i] if i < len(cells) else "").ljust(widths[i]) for i in range(cols)
        )

    lines = [fmt(head), "-+-".join("-" * w for w in widths), *(fmt(r) for r in rows)]
    start = b.mark()
    b.text("\n".join(lines))
    b.span(lambda o, ln: MessageEntityPre(o, ln, ""), start)


def _render_list(b: _Builder, tok: dict, depth: int = 0) -> None:
    ordered = tok["attrs"]["ordered"]
    num = tok["attrs"].get("start", 1) or 1
    indent = "  " * depth
    for i, item in enumerate(tok["children"]):
        if i > 0:
            b.text("\n")
        b.text(f"{indent}{num}. " if ordered else f"{indent}• ")
        num += 1
        for child in item["children"]:
            ct = child["type"]
            if ct in ("block_text", "paragraph"):
                _inline(b, child["children"])
            elif ct == "list":
                b.text("\n")
                _render_list(b, child, depth + 1)
            else:
                _block(b, child)


def _block(b: _Builder, tok: dict) -> None:
    t = tok["type"]
    if t in ("paragraph", "block_text"):
        _inline(b, tok["children"])
    elif t == "heading":
        start = b.mark()
        _inline(b, tok["children"])
        b.span(MessageEntityBold, start)  # Telegram has no heading; emulate as bold
    elif t == "block_code":
        lang = (tok.get("attrs") or {}).get("info") or ""
        lang = lang.split()[0] if lang else ""
        start = b.mark()
        b.text(tok["raw"].rstrip("\n"))
        b.span(lambda o, ln, la=lang: MessageEntityPre(o, ln, la), start)
    elif t == "block_quote":
        start = b.mark()
        _blocks(b, tok["children"])
        b.span(MessageEntityBlockquote, start)
    elif t == "list":
        _render_list(b, tok)
    elif t == "table":
        _render_table(b, tok)
    elif "children" in tok:
        _inline(b, tok["children"])


def _blocks(b: _Builder, tokens: list, sep: str = "\n\n") -> None:
    first = True
    for tok in tokens:
        if tok["type"] == "blank_line":
            continue
        if not first:
            b.text(sep)
        first = False
        _block(b, tok)


def render(markdown_text: str) -> tuple[str, list]:
    """Render Markdown to (plain text, list of Telethon MessageEntity)."""
    b = _Builder()
    _blocks(b, _MD(markdown_text))
    return b.result()
