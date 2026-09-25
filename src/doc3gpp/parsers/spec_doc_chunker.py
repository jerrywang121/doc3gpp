"""Pure block->chunk splitter. Break priority paragraph -> sentence -> table-row; rows atomic."""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from doc3gpp.models.spec_doc import ChunkDraft
from doc3gpp.parsers.docx_converter import (
    Block,
    HeadingBlock,
    ParagraphBlock,
    TableBlock,
    _split_table_caption,
)

_SENT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class _SourceUnit:
    text: str
    sections: tuple[str, ...]
    tables: tuple[str, ...]
    file_order: int
    source_file: str
    atomic: bool
    join_before: str = "\n\n"
    tokens: tuple[str, ...] = field(init=False)
    token_metadata: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = field(
        init=False
    )

    def __post_init__(self) -> None:
        tokens = tuple(self.text.split())
        object.__setattr__(self, "tokens", tokens)
        object.__setattr__(
            self,
            "token_metadata",
            tuple(
                (self.sections, self.tables, self.join_before if i == 0 else "")
                for i, _ in enumerate(tokens)
            ),
        )

    @property
    def token_count(self) -> int:
        return len(self.tokens)


def _sentences(text: str) -> list[str]:
    return [s for s in _SENT_RE.split(text.strip()) if s]


def chunk_blocks(blocks: list[Block], chunk_size: int = 512, chunk_overlap: int = 24,
                 max_chunk_chars: int = 1500, *, file_order: int = 0,
                 source_file: str = "") -> list[ChunkDraft]:
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be > 0, got {chunk_size}")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be < chunk_size")
    units: list[_SourceUnit] = []
    cur_section = None
    cur_file_order, cur_file = file_order, source_file

    def label(number: str | None, title: str | None) -> str:
        return f"{number + ' ' if number else ''}{title or ''}".strip()

    for block_index, b in enumerate(blocks):
        if isinstance(b, HeadingBlock):
            cur_section = label(b.section_no, b.title)
            sections = (cur_section,) if cur_section else ()
            units.append(_SourceUnit(
                b.raw,
                sections,
                (),
                cur_file_order,
                cur_file,
                False,
            ))
        elif isinstance(b, ParagraphBlock):
            tables: tuple[str, ...] = ()
            caption_no, caption_title = _split_table_caption(b.text)
            if caption_no is not None:
                for adjacent_index in (block_index - 1, block_index + 1):
                    if 0 <= adjacent_index < len(blocks):
                        adjacent = blocks[adjacent_index]
                        if isinstance(adjacent, TableBlock):
                            table_label = label(adjacent.table_no, adjacent.table_title)
                            if table_label:
                                tables = (table_label,)
                            break
            for s in _sentences(b.text) or [b.text]:
                sections = (cur_section,) if cur_section else ()
                # A single sentence can still exceed either budget: split its tokens into
                # windows bounded by chunk_size tokens AND max_chunk_chars chars.
                toks = s.split()
                if len(toks) > chunk_size or len(s) > max_chunk_chars:
                    start = 0
                    while start < len(toks):
                        acc: list[str] = []
                        acc_chars = 0
                        while start + len(acc) < len(toks) and len(acc) < chunk_size:
                            w = toks[start + len(acc)]
                            add = len(w) + (1 if acc else 0)
                            if acc and acc_chars + add > max_chunk_chars:
                                break
                            acc.append(w)
                            acc_chars += add
                        if not acc:
                            acc.append(toks[start])
                            acc_chars = len(acc[0])  # single huge token: emit alone
                        units.append(_SourceUnit(
                            " ".join(acc),
                            sections,
                            tables,
                            cur_file_order,
                            cur_file,
                            False,
                            " " if start else "\n\n",
                        ))
                        start += len(acc)
                else:
                    units.append(_SourceUnit(
                        s, sections, tables, cur_file_order, cur_file, False
                    ))
        elif isinstance(b, TableBlock):
            lines = b.gfm.splitlines()
            header = "\n".join(lines[:2]) if len(lines) >= 2 else (lines[0] if lines else "")
            rows = lines[2:] if len(lines) > 2 else []
            whole = b.gfm
            tables = (label(b.table_no, b.table_title),)
            sections = (cur_section,) if cur_section else ()
            # Fitting = fits the char ceiling: kept whole even when tokens overflow
            # chunk_size (allowed, like TDoc's whole-table preference).
            if len(whole) <= max_chunk_chars:
                units.append(_SourceUnit(
                    whole, sections, tables, cur_file_order, cur_file, True
                ))
            else:
                for r in rows:
                    if not r.strip():
                        continue
                    row_text = header + "\n" + r if header else r
                    units.append(_SourceUnit(
                        row_text, sections, tables, cur_file_order, cur_file, True
                    ))
    chunks: list[ChunkDraft] = []
    chunk_token_metadata: list[
        list[tuple[tuple[str, ...], tuple[str, ...], str]]
    ] = []
    cur: list[str] = []
    cur_tokens = 0
    cur_sections: list[str] = []
    cur_tables: list[str] = []
    cur_token_metadata: list[tuple[tuple[str, ...], tuple[str, ...], str]] = []
    cur_file_order, cur_file = file_order, source_file

    def add_metadata(sections: tuple[str, ...], tables: tuple[str, ...]) -> None:
        for section in sections:
            if section and section not in cur_sections:
                cur_sections.append(section)
        for table in tables:
            if table and table not in cur_tables:
                cur_tables.append(table)

    def flush():
        nonlocal cur, cur_tokens, cur_sections, cur_tables, cur_token_metadata
        if cur:
            chunks.append(ChunkDraft(
                file_order=cur_file_order,
                source_file=cur_file,
                sections="\n".join(cur_sections) or None,
                tables="\n".join(cur_tables) or None,
                text="\n\n".join(cur).strip(),
            ))
            chunk_token_metadata.append(cur_token_metadata)
        cur, cur_tokens = [], 0
        cur_sections, cur_tables = [], []
        cur_token_metadata = []

    for unit in units:
        toks = unit.token_count
        if unit.atomic:
            if cur and (cur_tokens + toks > chunk_size or len("\n\n".join(cur)) + len(unit.text) > max_chunk_chars):
                flush()
            # oversized single row: emit alone (rows are atomic, never split mid-row)
            if toks > chunk_size or len(unit.text) > max_chunk_chars:
                flush()
                chunks.append(ChunkDraft(
                    file_order=unit.file_order,
                    source_file=unit.source_file,
                    sections="\n".join(unit.sections) or None,
                    tables="\n".join(unit.tables) or None,
                    text=unit.text,
                ))
                chunk_token_metadata.append(list(unit.token_metadata))
                continue
            add_metadata(unit.sections, unit.tables)
            cur.append(unit.text)
            cur_tokens += toks
            cur_token_metadata.extend(unit.token_metadata)
            continue
        if cur and (cur_tokens + toks > chunk_size or len("\n\n".join(cur)) + len(unit.text) + 2 > max_chunk_chars):
            flush()
        add_metadata(unit.sections, unit.tables)
        cur.append(unit.text)
        cur_tokens += toks
        cur_token_metadata.extend(unit.token_metadata)
    flush()
    # overlap: prepend an exact trailing substring from the previous chunk
    if chunk_overlap > 0:
        for i in range(1, len(chunks)):
            prev_text = chunks[i - 1].text
            token_matches = list(re.finditer(r"\S+", prev_text))
            if token_matches:
                overlap_start = max(0, len(token_matches) - chunk_overlap)
                overlap_matches = token_matches[overlap_start:]
                overlap_toks = [match.group() for match in overlap_matches]
                prefix = prev_text[overlap_matches[0].start():]
                next_toks = chunks[i].text.split()
                if next_toks[:len(overlap_toks)] != overlap_toks:
                    join_before = (
                        chunk_token_metadata[i][0][2]
                        if chunk_token_metadata[i]
                        else "\n\n"
                    )
                    chunks[i].text = prefix + join_before + chunks[i].text
                    overlap_metadata = chunk_token_metadata[i - 1][-len(overlap_matches):]
                    chunk_token_metadata[i] = overlap_metadata + chunk_token_metadata[i]
                    for field_index, field in enumerate(("sections", "tables")):
                        overlap_entries: list[str] = []
                        for metadata in overlap_metadata:
                            for entry in metadata[field_index]:
                                if entry and entry not in overlap_entries:
                                    overlap_entries.append(entry)
                        current = (getattr(chunks[i], field) or "").splitlines()
                        merged: list[str] = []
                        for entry in overlap_entries + current:
                            if entry and entry not in merged:
                                merged.append(entry)
                        setattr(chunks[i], field, "\n".join(merged) or None)
    return [c for c in chunks if c.text.strip()]
