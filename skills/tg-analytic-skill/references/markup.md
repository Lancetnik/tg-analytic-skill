# Post markup reference

Read this before writing a post body for `tg_publish.py`
(`schedule`/`edit`). The body is a Markdown file; `_md2entities.py` walks
mistune's AST straight to Telegram `MessageEntity` offsets — there is **no HTML
step**, so HTML tags are not interpreted (write Markdown, not `<b>`).

## Supported markup

| Markdown | Renders as | Notes |
| --- | --- | --- |
| `**bold**` | bold | |
| `*italic*` or `_italic_` | italic | |
| `~~strike~~` | strikethrough | |
| `^^underline^^` | underline | mistune `insert` syntax — **not** `<u>` |
| `\|\|spoiler\|\|` | hidden text | custom inline rule (Telegram's own spoiler syntax) |
| `` `code` `` | inline monospace | |
| ```` ```lang\n…\n``` ```` | code block | language label preserved (e.g. ` ```python `) |
| `[text](https://url)` | inline link | |
| `> line` | blockquote | consecutive `>` lines join into one quote |
| `- a` / `* a` | bulleted list | rendered with `•` |
| `1. a` | numbered list | |
| nested list (indent 2 spaces) | nested list | |
| `# Heading` … `###### ` | **bold line** | Telegram has no heading entity |
| Markdown table (`\| a \| b \|`) | aligned **monospace** block | Telegram has no table entity |

Formats **nest/overlap** correctly — e.g. `**bold with a [link](url)**`, a
blockquote containing `**bold**` and `||spoiler||`, or a list item with mixed
styles. Offsets are tracked in UTF-16 units, so emoji and Cyrillic stay aligned.

## Passed through literally

- `#hashtags` — kept as text; Telegram auto-links them client-side. (This is
  why the parser is mistune, not Python-Markdown: `#word` must **not** become a
  heading.)
- Emoji — any unicode emoji, inline anywhere.
- A lone `<` or `>` in prose (e.g. `5 < 10`) — stays literal.

## Not available in a Telegram message

These exist in Telegram's Instant-View `RichText`, never in a sent message, so
there is no Markdown for them and they cannot be produced:

- subscript / superscript
- highlight / marker (`==mark==` has no message entity)

Tables and headings have no native entity either; they are emulated (monospace
block, bold line) as noted above. Raw HTML is not parsed.

## Tips

- A list may follow a paragraph **without** a blank line in between.
- For tabular data, a Markdown table or a fenced code block both render as a
  monospace block — pick whichever reads better.
- The body comes from `--file PATH` or stdin (`--file -`, or omit `--file`);
  either way it is published **verbatim** — there is no front-matter/metainfo
  stripping. Pass only the clean body. When a draft also holds a header or
  notes, produce the body and pipe it via a quoted heredoc (`--file - <<'EOF'`)
  rather than writing a temp file.
