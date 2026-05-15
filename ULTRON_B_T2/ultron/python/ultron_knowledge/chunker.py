"""Markdown-aware chunker.

Splits a note by H2/H3 headings, then merges adjacent short sections so
no chunk is microscopic. Caps at roughly `max_chars` characters per
chunk — sentence-transformers' MiniLM truncates at 256 tokens (~1024
chars), and slightly larger chunks degrade gracefully.

Output: list of (chunk_text, heading_path) tuples. `heading_path` is a
short "Note Title › Section › Subsection" string used to give the LLM
context about where each retrieved chunk lives.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Treat H1 as the note's title (one per file); split chunks on H2/H3.
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    text: str
    heading_path: str


def chunk_markdown(content: str, max_chars: int = 1600, min_chars: int = 200) -> list[Chunk]:
    """Split a markdown note into chunks with heading-path context."""
    content = content.strip()
    if not content:
        return []

    title_match = _H1_RE.search(content)
    title = title_match.group(1).strip() if title_match else "(untitled)"

    # Tokenise into (heading_level, heading_text, start_offset, end_offset) sections.
    sections: list[tuple[int, str, int, int]] = []
    last_end = 0
    last_level = 0
    last_heading = title
    matches = list(_HEADING_RE.finditer(content))
    if not matches:
        # No headings — treat the whole file as one section under the title.
        sections.append((1, title, 0, len(content)))
    else:
        # Preamble before the first heading.
        if matches[0].start() > 0:
            sections.append((1, title, 0, matches[0].start()))
        for i, m in enumerate(matches):
            level = len(m.group(1))
            heading = m.group(2).strip()
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            sections.append((level, heading, start, end))

    # Build heading path stack so each section knows its ancestors.
    chunks: list[Chunk] = []
    path_stack: list[str] = [title]
    for level, heading, start, end in sections:
        # Pop deeper headings off the stack until we find the parent.
        while len(path_stack) > level:
            path_stack.pop()
        # If level==1, replace the title slot rather than appending under it.
        if level == 1:
            path_stack = [heading]
        else:
            if len(path_stack) == level:
                path_stack[-1] = heading
            else:
                # Skipping levels (## then #### with no ###) — pad with placeholders.
                while len(path_stack) < level - 1:
                    path_stack.append("")
                path_stack.append(heading)

        body = content[start:end].strip()
        if not body:
            continue
        # If the section text starts with the heading itself, keep it —
        # the embedding benefits from the heading words.
        path_str = " › ".join(p for p in path_stack if p)
        # Split very long sections on blank-line boundaries.
        for piece in _split_long(body, max_chars):
            chunks.append(Chunk(text=piece, heading_path=path_str))

    # Merge tiny adjacent chunks under the same parent heading.
    merged: list[Chunk] = []
    for c in chunks:
        if merged and len(merged[-1].text) + len(c.text) < min_chars * 2 and merged[-1].heading_path == c.heading_path:
            merged[-1] = Chunk(text=merged[-1].text + "\n\n" + c.text, heading_path=c.heading_path)
        else:
            merged.append(c)
    return merged


def _split_long(text: str, max_chars: int) -> list[str]:
    """Split a long block on blank lines without exceeding max_chars."""
    if len(text) <= max_chars:
        return [text]
    parts = text.split("\n\n")
    out: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for p in parts:
        if buf and buf_len + len(p) > max_chars:
            out.append("\n\n".join(buf))
            buf, buf_len = [p], len(p)
        else:
            buf.append(p)
            buf_len += len(p)
    if buf:
        out.append("\n\n".join(buf))
    # Hard-cap any monster paragraph by raw slicing.
    final: list[str] = []
    for piece in out:
        if len(piece) <= max_chars * 1.5:
            final.append(piece)
        else:
            for i in range(0, len(piece), max_chars):
                final.append(piece[i:i + max_chars])
    return final
