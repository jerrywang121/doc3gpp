from __future__ import annotations

import pytest

from doc3gpp.services.embedding.chunker import (
    CHUNK_OVERLAP_DEFAULT,
    CHUNK_SIZE_DEFAULT,
    _chunks,
)


def test_defaults():
    assert CHUNK_SIZE_DEFAULT == 200
    assert CHUNK_OVERLAP_DEFAULT == 20


def test_empty_string_returns_empty_list():
    assert _chunks("", 800, 100) == []


def test_whitespace_only_returns_empty_list():
    assert _chunks("   \n\t  ", 800, 100) == []


def test_shorter_than_size_returns_single_chunk_stripped():
    text = "  hello world  "
    assert _chunks(text, 800, 100) == ["hello world"]


def test_exact_size_single_chunk():
    text = " ".join(f"tok{i}" for i in range(800))
    chunks = _chunks(text, 800, 100)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_two_chunks_with_overlap():
    # 900 tokens, size=800, overlap=100 → chunk0 = [0,800), chunk1 = [700,900)
    text = " ".join(f"tok{i}" for i in range(900))
    chunks = _chunks(text, 800, 100)
    assert len(chunks) == 2
    chunk0_tokens = chunks[0].split()
    chunk1_tokens = chunks[1].split()
    assert len(chunk0_tokens) == 800
    assert len(chunk1_tokens) == 200
    # overlap: chunk1 starts at token 700
    assert chunk1_tokens[0] == "tok700"
    assert chunk1_tokens[-1] == "tok899"
    assert chunk0_tokens[700] == "tok700"  # overlap point


def test_three_chunks_chain():
    # 2000 tokens, size=800, overlap=100 →
    # chunk0 = [0,800), chunk1 = [700,1500), chunk2 = [1400,2000)
    text = " ".join(f"tok{i}" for i in range(2000))
    chunks = _chunks(text, 800, 100)
    assert len(chunks) == 3
    assert chunks[0].split()[0] == "tok0"
    assert chunks[1].split()[0] == "tok700"
    assert chunks[2].split()[0] == "tok1400"
    assert chunks[2].split()[-1] == "tok1999"


def test_overlap_zero():
    text = " ".join(f"tok{i}" for i in range(1600))
    chunks = _chunks(text, 800, 0)
    assert len(chunks) == 2
    assert chunks[0].split()[-1] == "tok799"
    assert chunks[1].split()[0] == "tok800"


def test_overlap_must_be_less_than_size():
    with pytest.raises(ValueError, match="overlap"):
        _chunks("a b c", size=5, overlap=5)
    with pytest.raises(ValueError, match="overlap"):
        _chunks("a b c", size=5, overlap=6)


def test_size_must_be_positive():
    with pytest.raises(ValueError, match="size"):
        _chunks("a b c", size=0, overlap=0)
    with pytest.raises(ValueError, match="size"):
        _chunks("a b c", size=-1, overlap=0)


def test_trailing_whitespace_stripped_per_chunk():
    text = " ".join(f"tok{i}" for i in range(800)) + "   "
    chunks = _chunks(text, 800, 100)
    assert len(chunks) == 1
    assert chunks[0] == chunks[0].strip()
    assert not chunks[0].endswith("   ")
