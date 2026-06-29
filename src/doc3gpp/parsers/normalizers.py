from __future__ import annotations


def clean_whitespace(value: str) -> str:
    """Normalize repeated whitespace in extracted text."""

    return " ".join(value.split())
