from __future__ import annotations

import sys

import pytest

from doc3gpp.models.semantic_search import SpacyUnavailableError
from doc3gpp.services.embedding.stopwords import strip_stopwords


def _spacy_available():
    try:
        import spacy  # noqa: F401
        return True
    except ImportError:
        return False


SEMANTIC = pytest.mark.semantic
SPACY_SKIP = pytest.mark.skipif(
    not _spacy_available(),
    reason="spacy or en_core_web_sm is not installed",
)


@SEMANTIC
@SPACY_SKIP
def test_strip_drops_punctuation_and_stopwords(monkeypatch):
    # "the" and "is" are spaCy stopwords; "CR" and "NB-IoT" are not.
    out = strip_stopwords("the CR is about NB-IoT")
    tokens = out.split()
    assert "the" not in tokens
    assert "is" not in tokens
    assert "cr" in tokens  # lowercased lemma
    assert "nb-iot" in tokens or "nb" in tokens  # tokenizer-dependent


@SEMANTIC
@SPACY_SKIP
def test_strip_emits_lemmas(monkeypatch):
    out = strip_stopwords("CRs touching power saving")
    tokens = out.split()
    # lemma of "touching" is "touch"; "saving" -> "save"
    assert "touch" in tokens
    assert "save" in tokens


@SEMANTIC
@SPACY_SKIP
def test_punctuation_only_returns_empty():
    assert strip_stopwords("  ... --- !!!  ") == ""


@SEMANTIC
@SPACY_SKIP
def test_keep_negation_words_default_retains_not(monkeypatch):
    # Default keep_negation_words=["not"] -> "not" must survive.
    out = strip_stopwords("which CRs do not relate to NB-IoT")
    tokens = out.split()
    assert "not" in tokens


@SEMANTIC
@SPACY_SKIP
def test_keep_negation_words_empty_strips_not(monkeypatch):
    # Simulate settings with keep_negation_words=[]
    from doc3gpp.services.embedding import stopwords as sw
    sw._cached_stopwords = None  # force recompute
    monkeypatch.setattr(
        "doc3gpp.settings.loader.get_settings",
        lambda: _make_settings(keep_negation_words=[]),
    )
    out = strip_stopwords("which CRs do not relate to NB-IoT")
    tokens = out.split()
    assert "not" not in tokens


@SEMANTIC
@SPACY_SKIP
def test_user_defined_stop_words_drops_token(monkeypatch):
    from doc3gpp.services.embedding import stopwords as sw
    sw._cached_stopwords = None
    monkeypatch.setattr(
        "doc3gpp.settings.loader.get_settings",
        lambda: _make_settings(user_defined_stop_words=["tdoc"]),
    )
    out = strip_stopwords("tdoc CR agenda")
    tokens = out.split()
    assert "tdoc" not in tokens
    assert "cr" in tokens


@SEMANTIC
@SPACY_SKIP
def test_user_defined_stop_words_default_empty_keeps_token():
    # Default user_defined_stop_words=[] -> "tdoc" survives.
    from doc3gpp.services.embedding import stopwords as sw
    sw._cached_stopwords = None
    out = strip_stopwords("tdoc CR agenda")
    tokens = out.split()
    assert "tdoc" in tokens


def test_empty_string_returns_empty():
    assert strip_stopwords("") == ""


def test_whitespace_only_returns_empty():
    assert strip_stopwords("   \n\t  ") == ""


def test_spacy_missing_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "spacy", None)
    from doc3gpp.services.embedding import stopwords as sw
    sw._cached_pipeline = None
    with pytest.raises(SpacyUnavailableError):
        strip_stopwords("some query")


class _FakeSearch:
    def __init__(self, **kw):
        self.semantic_search = type("S", (), kw)()


class _FakeSettings:
    def __init__(self, **kw):
        self.search = type("S", (), {"enabled": True})()
        self.semantic_search = type("S", (), kw)()


def _make_settings(*, user_defined_stop_words=None, keep_negation_words=None):
    return _FakeSettings(
        semantic_search=type(
            "S", (),
            {
                "user_defined_stop_words": user_defined_stop_words or [],
                "keep_negation_words": keep_negation_words
                if keep_negation_words is not None
                else ["not"],
            },
        )(),
    )
