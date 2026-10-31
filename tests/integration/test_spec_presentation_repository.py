"""Persistence contract for compressed spec-presentation Markdown."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

import doc3gpp.models.spec_doc as spec_doc_models
from doc3gpp.models.spec_doc import ChunkDraft
from doc3gpp.storage.compression import decompress_json
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_specdata_engine
from doc3gpp.storage.repositories.spec_doc_sql import SQLAlchemySpecDocRepository


@pytest.fixture()
def repo(sqlite_env):
    create_schema("specdata")
    return SQLAlchemySpecDocRepository()


@pytest.fixture()
def raw_specdata_connection(repo):
    with get_specdata_engine().connect() as connection:
        yield connection


def _presentation(
    *,
    source_file: str,
    file_order: int = 0,
    markdown: str = "",
    spec_id: str = "38.331",
    version: str = "18.5.0",
):
    presentation_type = getattr(spec_doc_models, "SpecDocPresentation", None)
    assert presentation_type is not None, "SpecDocPresentation DTO is missing"
    return presentation_type(spec_id, version, source_file, file_order, markdown)



def _replace_version_content(repo, *args, **kwargs):
    replacement = getattr(repo, "replace_version_content", None)
    assert replacement is not None, "SpecDocRepository.replace_version_content is missing"
    return replacement(*args, **kwargs)


def test_presentation_is_a_slots_dto() -> None:
    row = _presentation(source_file="slides/a.pptx")

    assert not hasattr(row, "__dict__")


def test_roundtrip_is_gzip_json_and_exact(repo, raw_specdata_connection) -> None:
    row = _presentation(
        source_file="slides/a.pptx",
        file_order=1,
        markdown="## Slide 1\n日本語",
    )

    _replace_version_content(repo,
        "38.331", "18.5.0", release="Rel-18", drafts=[], presentations=[row]
    )

    assert repo.get_presentation("38.331", "18.5.0", "slides/a.pptx") == row
    assert repo.get_presentation("38.331", "18.5.0", "a.pptx") is None
    blob = raw_specdata_connection.execute(
        text("SELECT content FROM spec_doc_presentations")
    ).scalar_one()
    assert blob.startswith(b"\x1f\x8b")
    assert decompress_json(blob) == row.markdown


def test_list_presentations_uses_deterministic_file_order(repo) -> None:
    rows = [
        _presentation(source_file="slides/third.pptx", file_order=3),
        _presentation(source_file="slides/first.pptx", file_order=1),
        _presentation(source_file="slides/second.pptx", file_order=2),
    ]
    _replace_version_content(repo,
        "38.331", "18.5.0", release="Rel-18", drafts=[], presentations=rows
    )

    expected = ["slides/first.pptx", "slides/second.pptx", "slides/third.pptx"]
    assert [row.source_file for row in repo.list_presentations("38.331", "18.5.0")] == expected
    assert [row.source_file for row in repo.list_presentations("38.331", "18.5.0")] == expected


def test_empty_markdown_survives_roundtrip(repo) -> None:
    row = _presentation(source_file="slides/empty.pptx", markdown="")
    _replace_version_content(repo,
        "38.331", "18.5.0", release="Rel-18", drafts=[], presentations=[row]
    )

    assert repo.get_presentation("38.331", "18.5.0", row.source_file) == row


def test_empty_presentation_replacement_removes_prior_files_and_replaces_chunks(repo) -> None:
    _replace_version_content(repo,
        "38.331",
        "18.5.0",
        release="Rel-18",
        drafts=[ChunkDraft(0, "slides/a.pptx", text="old chunk")],
        presentations=[_presentation(source_file="slides/a.pptx", markdown="old slide")],
    )

    _replace_version_content(repo,
        "38.331",
        "18.5.0",
        release="Rel-18",
        drafts=[ChunkDraft(0, "slides/a.pptx", text="new chunk")],
        presentations=[],
    )

    assert repo.list_presentations("38.331", "18.5.0") == []
    assert [chunk.text for chunk in repo.list_chunks("38.331", version="18.5.0")] == [
        "new chunk"
    ]


def test_replacing_one_version_preserves_other_version_presentations(repo) -> None:
    other = _presentation(
        source_file="slides/other.pptx", version="18.6.0", markdown="other version"
    )
    _replace_version_content(repo,
        "38.331", "18.5.0", release="Rel-18", drafts=[], presentations=[
            _presentation(source_file="slides/current.pptx", markdown="old current")
        ]
    )
    _replace_version_content(repo,
        "38.331", "18.6.0", release="Rel-18", drafts=[], presentations=[other]
    )

    current = _presentation(
        source_file="slides/current.pptx", markdown="new current", file_order=1
    )
    _replace_version_content(repo,
        "38.331", "18.5.0", release="Rel-18", drafts=[], presentations=[current]
    )

    assert repo.list_presentations("38.331", "18.5.0") == [current]
    assert repo.list_presentations("38.331", "18.6.0") == [other]


def test_duplicate_presentation_keys_are_rejected(repo) -> None:
    first = _presentation(source_file="slides/a.pptx", markdown="first")
    duplicate = _presentation(
        source_file="slides/a.pptx", file_order=1, markdown="duplicate"
    )

    with pytest.raises(IntegrityError):
        _replace_version_content(repo,
            "38.331",
            "18.5.0",
            release="Rel-18",
            drafts=[],
            presentations=[first, duplicate],
        )


@pytest.mark.parametrize(
    ("spec_id", "version"),
    [("36.579-5", "18.5.0"), ("38.331", "18.4.0")],
)
def test_replacement_rejects_presentations_for_another_version(
    repo, spec_id: str, version: str
) -> None:
    row = _presentation(
        source_file="slides/a.pptx", spec_id=spec_id, version=version
    )

    with pytest.raises(ValueError):
        _replace_version_content(repo,
            "38.331", "18.5.0", release="Rel-18", drafts=[], presentations=[row]
        )


def test_specdata_schema_bootstrap_is_additive_and_idempotent(repo) -> None:
    row = _presentation(source_file="slides/a.pptx", markdown="stored")
    _replace_version_content(repo,
        "38.331", "18.5.0", release="Rel-18", drafts=[], presentations=[row]
    )

    create_schema("specdata")

    assert repo.list_presentations("38.331", "18.5.0") == [row]


def test_presentation_schema_has_the_expected_columns(repo) -> None:
    inspector = inspect(get_specdata_engine())
    assert inspector.has_table("spec_doc_presentations"), "presentation table is missing"
    columns = {
        column["name"]: column
        for column in inspector.get_columns("spec_doc_presentations")
    }

    assert list(columns) == ["spec_id", "version", "source_file", "file_order", "content"]
    assert columns["content"]["nullable"] is False


def test_failed_presentation_insert_rolls_back_old_chunks_and_presentations(repo) -> None:
    old_presentation = _presentation(
        source_file="slides/old.pptx", markdown="old presentation"
    )
    _replace_version_content(repo,
        "38.331",
        "18.5.0",
        release="Rel-18",
        drafts=[ChunkDraft(0, "slides/old.pptx", text="old chunk")],
        presentations=[old_presentation],
    )

    with get_specdata_engine().begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER fail_presentation_insert "
                "BEFORE INSERT ON spec_doc_presentations "
                "BEGIN SELECT RAISE(ABORT, 'presentation insert failed'); END"
            )
        )

    with pytest.raises(IntegrityError):
        _replace_version_content(repo,
            "38.331",
            "18.5.0",
            release="Rel-18",
            drafts=[ChunkDraft(0, "slides/new.pptx", text="new chunk")],
            presentations=[_presentation(source_file="slides/new.pptx", markdown="new")],
        )

    assert [chunk.text for chunk in repo.list_chunks("38.331", version="18.5.0")] == [
        "old chunk"
    ]
    assert repo.list_presentations("38.331", "18.5.0") == [old_presentation]
