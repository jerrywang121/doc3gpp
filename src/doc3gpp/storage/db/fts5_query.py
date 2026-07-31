"""Index-time query normalizer for the FTS5 search subsystem.

Python's bundled sqlite (3.45.1, current as of 2026) lacks
``ENABLE_FTS5_TOKENIZER`` and rejects ``tokenchars`` directives,
so we cannot register a custom FTS5 tokenizer at the sqlite level.
The compromise: use stock ``unicode61`` (which naturally splits
TDoc ids on the hyphen, splits ``NB-IoT`` into ``nb`` / ``iot``,
etc.) and run this pure-Python pre-processor at INDEX TIME on
every text column before INSERT.

Two recognition rules are added by this module on top of what
``unicode61`` already does:

1. **TDoc ID base + full id duplication** — ``R5-1234567r2`` is
   written as ``R5-1234567 R5-1234567r2`` so a search for the
   base id (``R5-1234567``) finds every revision AND a search
   for the specific revision (``R5-1234567r2``) also matches.
   ``unicode61`` alone would split the full id into three
   tokens (``r5``, ``1234567``, ``r2``).
2. **Spec id preservation** — ``38.300`` is written as ``38_300``
   so the full spec id stays one token. ``unicode61`` would
   split on the dot into ``38`` and ``300``.

The pre-processor is applied to every FTS5 column at index time
(``SQLAlchemySearchIndexRepository._build_index_text``) and to
the user-input query at search time (``SearchQueryBuilder.build``
in ``cli_filters.py``) so both sides see identical normalization.

Test corpus: ``tests/unit/test_fts5_query.py``.
"""

from __future__ import annotations

import re


# TDoc ID with optional revision. Broader than the existing ``CR_ID_RE``
# in ``parsers/tdoc_parser.py:15`` / ``scraping/tdoc_zip_source.py:35``
# (which excludes ``P`` for plenary TDocs). The user explicitly scoped
# fixing the existing regex to a separate change.
TDOC_ID_BASE_RE = re.compile(
    r"(?P<base>[RSC][1-9P][-sw]\d{6,7})(?:(?P<rev>r\d+))?",
    re.IGNORECASE,
)


# Spec number (``38.300`` or ``38.300-1``). The matching character class
# deliberately stops at the first non-``.`` / non-``-`` / non-digit so
# we don't greedily consume ``38.300.1`` into one token.
SPEC_ID_RE = re.compile(r"\d+\.\d+(?:-\d+)?")


def normalize_query(text: str) -> str:
    """Return the index-time-normalized form of ``text``.

    Rules (from
    ``docs/superpowers/specs/2026-07-29-fts5-search-design.md``
    §"What `normalize_query` adds on top"):

    * Every TDoc ID match is replaced with ``<base> <full>`` so both
      the base and the full id are searchable tokens. Case is
      normalized so the category letter and the position-1 letter
      (e.g. the ``P`` in ``RP-``/``SP-``) are upper-case, the rest
      is lower-case. The corpus pins this behaviour.
    * Every spec ID match has its ``.`` replaced with ``_`` so the
      full spec id survives as one FTS5 token.
    * Hyphenated jargon (``NB-IoT``), plain words, whitespace, etc.
      pass through unchanged — ``unicode61`` handles the splits.

    Empty input returns the empty string.
    """
    if not text:
        return text

    # First pass: handle TDoc IDs. We need to scan for them at the
    # full text level because ``SPEC_ID_RE`` might overlap (e.g. a
    # spec number embedded in a longer string); we apply TDoc-ID
    # normalization first since it's more specific.
    def _tdoc_sub(match: re.Match[str]) -> str:
        base = match.group("base")
        # Normalize the base case: upper-case the category letter,
        # upper-case the position-1 letter (so the plenary ``P`` in
        # ``RP-``/``SP-`` survives), lower-case the rest. The corpus
        # pins this behaviour.
        base_norm = base[0].upper() + base[1].upper() + base[2:].lower()
        full = match.group(0)
        full_norm = full[0].upper() + full[1].upper() + full[2:].lower()
        rev = match.group("rev")
        if rev is None:
            # No revision — emit the base twice so the base is still
            # a token in the index (the spec corpus pins this).
            return f"{base_norm} {base_norm}"
        return f"{base_norm} {full_norm}"

    text = TDOC_ID_BASE_RE.sub(_tdoc_sub, text)

    # Second pass: handle spec ids. Replace ``.`` with ``_`` so the
    # full id survives ``unicode61`` splitting.
    text = SPEC_ID_RE.sub(lambda m: m.group(0).replace(".", "_"), text)

    return text
