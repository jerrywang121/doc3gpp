"""spaCy-backed stopword + lemma pre-processor for the FTS5 path of
``search sem``.

The pipeline (``en_core_web_sm``) is loaded once per process and cached
on the module. The effective stopword set is composed once per process
from ``spacy.lang.en.stop_words.STOP_WORDS ∪ user_defined_stop_words −
keep_negation_words`` and cached on the module; ``strip_stopwords``
does the membership check against the cached frozenset so the
per-call cost is dominated by ``Doc`` creation, not model load.

The spaCy model is bundled with the ``[semantic]`` extra (pinned
direct wheel URL in ``pyproject.toml``); if a user installs spaCy
outside of pip (e.g. conda) without the model, a missing model
raises :class:`SpacyUnavailableError` and the CLI tells the user
to run ``python -m spacy download en_core_web_sm``.
"""

from __future__ import annotations

from doc3gpp.models.semantic_search import SpacyUnavailableError

_cached_pipeline = None
_cached_stopwords: frozenset[str] | None = None
_cached_settings_key: tuple | None = None


def _get_spacy_pipeline():
    global _cached_pipeline
    if _cached_pipeline is not None:
        return _cached_pipeline
    try:
        import spacy
    except ImportError as exc:
        raise SpacyUnavailableError(
            "spaCy is not installed; run `pip install doc3gpp[semantic]`"
        ) from exc
    try:
        _cached_pipeline = spacy.load("en_core_web_sm")
    except OSError as exc:
        raise SpacyUnavailableError(
            "spaCy model 'en_core_web_sm' not installed; "
            "run `python -m spacy download en_core_web_sm`"
        ) from exc
    return _cached_pipeline


def _effective_stopwords() -> frozenset[str]:
    global _cached_stopwords, _cached_settings_key
    from doc3gpp.settings.loader import get_settings
    settings = get_settings()
    sem = settings.semantic_search
    key = (
        tuple(sem.user_defined_stop_words),
        tuple(sem.keep_negation_words),
    )
    if _cached_stopwords is not None and _cached_settings_key == key:
        return _cached_stopwords
    # spaCy 3.7+ removed `spacy.Defaults.stop_words`; the canonical
    # English stopword set now lives at
    # `spacy.lang.en.stop_words.STOP_WORDS`. Importing the symbol
    # (rather than poking a top-level attribute) keeps us
    # version-portable across the 3.7/3.8 series the [semantic]
    # extra supports.
    from spacy.lang.en.stop_words import STOP_WORDS
    base = set(STOP_WORDS)
    base -= {w.lower() for w in sem.keep_negation_words}
    base |= {w.lower() for w in sem.user_defined_stop_words}
    _cached_stopwords = frozenset(base)
    _cached_settings_key = key
    return _cached_stopwords


def strip_stopwords(text: str) -> str:
    """Run ``text`` through spaCy and return lowercased lemmas of
    non-stopword, alpha-numeric tokens.

    Empty / punctuation-only / whitespace-only input returns ``""``.
    """
    if not text or not text.strip():
        return ""
    nlp = _get_spacy_pipeline()
    stop = _effective_stopwords()
    doc = nlp(text)
    out: list[str] = []
    for tok in doc:
        if tok.is_space or tok.is_punct:
            continue
        lemma = tok.lemma_.lower()
        if not lemma.isalnum() and not any(c.isalnum() for c in lemma):
            continue
        if lemma in stop:
            continue
        if not lemma:
            continue
        out.append(lemma)
    return " ".join(out)
