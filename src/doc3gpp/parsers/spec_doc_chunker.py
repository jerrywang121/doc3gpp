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
    units: list[tuple[str, dict]] = []  # (text, meta)
    cur_sec_no, cur_sec_title = None, None
    cur_file_order, cur_file = file_order, source_file
    for b in blocks:
        if isinstance(b, HeadingBlock):
            cur_sec_no, cur_sec_title = b.section_no, b.title
            units.append((f"{b.section_no + ' ' if b.section_no else ''}{b.title}".strip(),
                dict(sec_no=cur_sec_no, sec_title=cur_sec_title, t_no=None, t_title=None, fo=cur_file_order, sf=cur_file)))
        elif isinstance(b, ParagraphBlock):
            for s in _sentences(b.text) or [b.text]:
                meta = dict(sec_no=cur_sec_no, sec_title=cur_sec_title, t_no=None, t_title=None, fo=cur_file_order, sf=cur_file)
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
                        units.append((" ".join(acc), dict(meta)))
                        start += len(acc)
                else:
                    units.append((s, meta))
        elif isinstance(b, TableBlock):
            lines = b.gfm.splitlines()
            header = "\n".join(lines[:2]) if len(lines) >= 2 else (lines[0] if lines else "")
            rows = lines[2:] if len(lines) > 2 else []
            whole = b.gfm
            # Fitting = fits the char ceiling: kept whole even when tokens overflow
            # chunk_size (allowed, like TDoc's whole-table preference).
            if len(whole) <= max_chunk_chars:
                units.append((whole, dict(sec_no=cur_sec_no, sec_title=cur_sec_title, t_no=b.table_no, t_title=b.table_title, fo=cur_file_order, sf=cur_file, atomic=True)))
            else:
                for r in rows:
                    if not r.strip():
                        continue
                    units.append((header + "\n" + r if header else r,
                        dict(sec_no=cur_sec_no, sec_title=cur_sec_title, t_no=b.table_no, t_title=b.table_title, fo=cur_file_order, sf=cur_file, atomic=True)))
    chunks: list[ChunkDraft] = []
    cur: list[str] = []
    cur_tokens = 0
    meta = None

    def flush():
        nonlocal cur, cur_tokens, meta
        if cur and meta is not None:
            chunks.append(ChunkDraft(file_order=meta["fo"], source_file=meta["sf"], section_no=meta["sec_no"],
                section_title=meta["sec_title"], table_no=meta["t_no"], table_title=meta["t_title"], text="\n\n".join(cur).strip()))
        cur, cur_tokens = [], 0
    for text, m in units:
        toks = len(text.split())
        if m.get("atomic"):
            if cur and (cur_tokens + toks > chunk_size or len("\n\n".join(cur)) + len(text) > max_chunk_chars):
                flush()
                meta = m
            if meta is None:
                meta = m
            # oversized single row: emit alone (rows are atomic, never split mid-row)
            if toks > chunk_size or len(text) > max_chunk_chars:
                flush()
                chunks.append(ChunkDraft(m["fo"], m["sf"], m["sec_no"], m["sec_title"], m["t_no"], m["t_title"], text))
                meta = None
                continue
            cur.append(text)
            cur_tokens += toks
            continue
        if meta is None:
            meta = m
        if cur and (cur_tokens + toks > chunk_size or len("\n\n".join(cur)) + len(text) + 2 > max_chunk_chars):
            flush()
            meta = m
        cur.append(text)
        cur_tokens += toks
    flush()
    # overlap: prepend trailing tokens of previous chunk
    if chunk_overlap > 0:
        for i in range(1, len(chunks)):
            prev_toks = chunks[i - 1].text.split()
            if prev_toks:
                prefix = " ".join(prev_toks[-chunk_overlap:])
                if not chunks[i].text.startswith(prefix):
                    chunks[i].text = prefix + " " + chunks[i].text
    return [c for c in chunks if c.text.strip()]
