"""Pure block->chunk splitter. Break priority paragraph -> sentence -> table-row; rows atomic."""
from __future__ import annotations
import re
from doc3gpp.models.spec_doc import ChunkDraft
from doc3gpp.parsers.docx_converter import Block, HeadingBlock, ParagraphBlock, TableBlock

_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str) -> list[str]:
    return [s for s in _SENT_RE.split(text.strip()) if s]


def chunk_blocks(blocks: list[Block], chunk_size: int = 512, chunk_overlap: int = 24,
                 max_chunk_chars: int = 1500, *, file_order: int = 0,
                 source_file: str = "") -> list[ChunkDraft]:
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be > 0, got {chunk_size}")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be < chunk_size")
    units: list[tuple[str, tuple[str, ...], tuple[str, ...], int, str, bool]] = []
    cur_section = None
    cur_file_order, cur_file = file_order, source_file

    def label(number: str | None, title: str | None) -> str:
        return f"{number + ' ' if number else ''}{title or ''}".strip()

    for b in blocks:
        if isinstance(b, HeadingBlock):
            cur_section = label(b.section_no, b.title)
        elif isinstance(b, ParagraphBlock):
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
                        units.append((" ".join(acc), sections, (), cur_file_order, cur_file, False))
                        start += len(acc)
                else:
                    units.append((s, sections, (), cur_file_order, cur_file, False))
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
                units.append((whole, sections, tables, cur_file_order, cur_file, True))
            else:
                for r in rows:
                    if not r.strip():
                        continue
                    units.append((header + "\n" + r if header else r, sections, tables,
                        cur_file_order, cur_file, True))
    chunks: list[ChunkDraft] = []
    chunk_token_metadata: list[
        list[tuple[tuple[str, ...], tuple[str, ...]]]
    ] = []
    cur: list[str] = []
    cur_tokens = 0
    cur_sections: list[str] = []
    cur_tables: list[str] = []
    cur_token_metadata: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
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

    for text, sections, tables, unit_file_order, unit_file, atomic in units:
        toks = len(text.split())
        if atomic:
            if cur and (cur_tokens + toks > chunk_size or len("\n\n".join(cur)) + len(text) > max_chunk_chars):
                flush()
            add_metadata(sections, tables)
            # oversized single row: emit alone (rows are atomic, never split mid-row)
            if toks > chunk_size or len(text) > max_chunk_chars:
                flush()
                chunks.append(ChunkDraft(
                    file_order=unit_file_order,
                    source_file=unit_file,
                    sections="\n".join(sections) or None,
                    tables="\n".join(tables) or None,
                    text=text,
                ))
                chunk_token_metadata.append([(sections, tables)] * toks)
                continue
            cur.append(text)
            cur_tokens += toks
            cur_token_metadata.extend([(sections, tables)] * toks)
            continue
        if cur and (cur_tokens + toks > chunk_size or len("\n\n".join(cur)) + len(text) + 2 > max_chunk_chars):
            flush()
        add_metadata(sections, tables)
        cur.append(text)
        cur_tokens += toks
        cur_token_metadata.extend([(sections, tables)] * toks)
    flush()
    # overlap: prepend trailing tokens of previous chunk
    if chunk_overlap > 0:
        for i in range(1, len(chunks)):
            prev_toks = chunks[i - 1].text.split()
            if prev_toks:
                prefix = " ".join(prev_toks[-chunk_overlap:])
                overlap_metadata = chunk_token_metadata[i - 1][-chunk_overlap:]
                if not chunks[i].text.startswith(prefix):
                    chunks[i].text = prefix + " " + chunks[i].text
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
