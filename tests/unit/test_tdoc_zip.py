"""Unit tests for TDoc ZIP member and derived Markdown archive primitives."""

from __future__ import annotations

import gzip
import hashlib
import importlib
import importlib.util
import io
import json
import zipfile

import pytest


def _tdoc_zip_module():
    """Load the archive API with an assertion-style missing-feature failure."""
    spec = importlib.util.find_spec("doc3gpp.parsers.tdoc_zip")
    assert spec is not None, "the TDoc ZIP primitives have not been implemented"
    return importlib.import_module("doc3gpp.parsers.tdoc_zip")


def _document_model():
    """Load ORM-free TDoc document models after asserting their module exists."""
    spec = importlib.util.find_spec("doc3gpp.models.tdoc_document")
    assert spec is not None, "the TDoc document value objects have not been implemented"
    return importlib.import_module("doc3gpp.models.tdoc_document")


def _make_zip(files: list[tuple[str, bytes]]) -> bytes:
    """Build an in-memory ZIP with fixed entry timestamps so its bytes are reproducible."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, payload in files:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(info, payload)
    return output.getvalue()


def _make_document(
    api,
    model,
    *,
    ftp_url: str,
    source_file: str,
    file_order: int,
    markdown_text: str,
):
    document_id = api.document_id_for(ftp_url, source_file)
    basename = source_file.replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0]
    return model.TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=f"documents/{document_id}/{stem}.md",
        file_order=file_order,
        markdown_text=markdown_text,
        content_hash=hashlib.sha256(markdown_text.encode("utf-8")).hexdigest(),
    )


def test_enumerate_docx_members_returns_sorted_full_paths_and_ignores_other_entries(
    caplog,
) -> None:
    api = _tdoc_zip_module()
    payload = _make_zip(
        [
            ("z/main.docx", b"main"),
            ("a/shared.docx", b"first support"),
            ("b/shared.docx", b"second support"),
            ("empty-folder/", b""),
            ("__MACOSX/._hidden.docx", b"resource fork"),
            ("docs/~$locked.docx", b"Word lock"),
            ("docs/manual.pdf", b"unsupported"),
        ]
    )

    with caplog.at_level("WARNING", logger="doc3gpp.parsers.tdoc_zip"):
        members = api.enumerate_docx_members(payload)

    assert members == (
        ("a/shared.docx", b"first support"),
        ("b/shared.docx", b"second support"),
        ("z/main.docx", b"main"),
    )
    assert members[0][0].rsplit("/", 1)[-1] == members[1][0].rsplit("/", 1)[-1]
    assert "docs/manual.pdf" in caplog.text


def test_enumerate_docx_members_normalizes_backslash_separators_only() -> None:
    api = _tdoc_zip_module()
    payload = _make_zip([("Folder\\Main.docx", b"docx")])

    assert api.enumerate_docx_members(payload) == (("Folder/Main.docx", b"docx"),)


@pytest.mark.parametrize(
    "member_name",
    [
        "/absolute.docx",
        "../escape.docx",
        "nested/../../escape.docx",
        "C:\\absolute.docx",
        "\\\\server\\share\\absolute.docx",
    ],
)
def test_enumerate_docx_members_rejects_absolute_or_traversing_names(
    member_name: str,
) -> None:
    api = _tdoc_zip_module()

    with pytest.raises(api.TDocZipValidationError):
        api.enumerate_docx_members(_make_zip([(member_name, b"unsafe")]))


def test_enumerate_docx_members_rejects_duplicate_names_after_separator_normalization() -> None:
    api = _tdoc_zip_module()
    payload = _make_zip(
        [
            ("folder/shared.docx", b"first"),
            ("folder\\shared.docx", b"second"),
        ]
    )

    with pytest.raises(api.TDocZipValidationError, match="duplicate"):
        api.enumerate_docx_members(payload)


def test_enumerate_docx_members_enforces_document_count_limit() -> None:
    api = _tdoc_zip_module()
    payload = _make_zip([("a.docx", b"a"), ("b.docx", b"b")])

    with pytest.raises(api.TDocZipLimitError, match="document"):
        api.enumerate_docx_members(payload, max_documents=1)


def test_enumerate_docx_members_enforces_declared_aggregate_size_limit() -> None:
    api = _tdoc_zip_module()
    payload = _make_zip([("a.docx", b"123"), ("b.docx", b"456")])

    with pytest.raises(api.TDocZipLimitError, match="uncompressed"):
        api.enumerate_docx_members(payload, max_uncompressed_bytes=5)


def test_enumerate_docx_members_enforces_actual_size_while_reading(monkeypatch) -> None:
    api = _tdoc_zip_module()
    payload = _make_zip([("a.docx", b"x")])
    original_open = zipfile.ZipFile.open

    def oversized_open(archive, name, *args, **kwargs):
        if isinstance(name, zipfile.ZipInfo) and name.filename == "a.docx":
            return io.BytesIO(b"more than the declared file size")
        return original_open(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", oversized_open)

    with pytest.raises(api.TDocZipLimitError, match="uncompressed"):
        api.enumerate_docx_members(payload, max_uncompressed_bytes=4)


def test_zero_limits_disable_document_and_uncompressed_byte_caps() -> None:
    api = _tdoc_zip_module()
    payload = _make_zip([("a.docx", b"123"), ("b.docx", b"456")])

    assert api.enumerate_docx_members(
        payload,
        max_documents=0,
        max_uncompressed_bytes=0,
    ) == (("a.docx", b"123"), ("b.docx", b"456"))


def test_document_id_uses_normalized_url_and_compact_utf8_json() -> None:
    api = _tdoc_zip_module()
    source_file = "Annex/Überblick.docx"
    serialized = json.dumps(
        ["tsg_ran/some.zip", source_file],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    assert api.document_id_for(
        "https://www.3gpp.org/ftp/TSG_RAN/Some.ZIP",
        source_file,
    ) == hashlib.sha256(serialized).hexdigest()


def test_domain_identity_matches_parser_compatibility_api() -> None:
    from doc3gpp.models.tdoc_identity import (
        document_id_for as domain_document_id_for,
    )

    api = _tdoc_zip_module()
    ftp_url = "https://www.3gpp.org/ftp/TSG_RAN/Some.ZIP"
    source_file = "Annex/Überblick.docx"

    assert domain_document_id_for(ftp_url, source_file) == api.document_id_for(
        ftp_url, source_file
    )


def test_tdoc_content_repository_does_not_import_parser_layer() -> None:
    import ast
    from pathlib import Path

    repository_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "doc3gpp"
        / "storage"
        / "repositories"
        / "tdoc_content_sql.py"
    )
    module = ast.parse(repository_path.read_text(encoding="utf-8"))
    parser_imports: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            parser_imports.update(
                alias.name
                for alias in node.names
                if alias.name.startswith("doc3gpp.parsers")
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith("doc3gpp.parsers")
        ):
            parser_imports.add(node.module)

    assert parser_imports == set()


def test_ephemeral_document_id_is_stable_and_uses_a_separate_namespace() -> None:
    api = _tdoc_zip_module()
    payload_hash = hashlib.sha256(b"local source zip").hexdigest()

    ephemeral_id = api.ephemeral_document_id_for(payload_hash, "Annex/main.docx")

    assert ephemeral_id == api.ephemeral_document_id_for(
        payload_hash,
        "Annex/main.docx",
    )
    assert ephemeral_id != api.document_id_for("local/source.zip", "Annex/main.docx")
    assert ephemeral_id != api.ephemeral_document_id_for(
        hashlib.sha256(b"different source zip").hexdigest(),
        "Annex/main.docx",
    )


def test_tdoc_document_preserves_full_source_path_and_validates_hashes() -> None:
    api = _tdoc_zip_module()
    model = _document_model()
    ftp_url = "tsg_ran/wg5/example.zip"
    source_file = "Annex\\Main.docx"
    markdown_text = "# Main document\n"
    document_id = api.document_id_for(ftp_url, source_file)
    normalized_source = "Annex/Main.docx"
    document = model.TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=f"documents/{document_id}/Main.md",
        file_order=0,
        markdown_text=markdown_text,
        content_hash=hashlib.sha256(markdown_text.encode("utf-8")).hexdigest(),
    )

    assert document.source_file == normalized_source
    assert document.document_id == document_id
    assert document.content_hash == hashlib.sha256(markdown_text.encode("utf-8")).hexdigest()

    with pytest.raises(ValueError, match="source_file"):
        model.TDocDocument(
            document_id=document_id,
            source_file="../unsafe.docx",
            markdown_file=f"documents/{document_id}/unsafe.md",
            file_order=0,
            markdown_text=markdown_text,
            content_hash=hashlib.sha256(markdown_text.encode("utf-8")).hexdigest(),
        )

    with pytest.raises(ValueError, match="content_hash"):
        model.TDocDocument(
            document_id=document_id,
            source_file="Annex/Main.docx",
            markdown_file=f"documents/{document_id}/Main.md",
            file_order=0,
            markdown_text=markdown_text,
            content_hash="0" * 64,
        )


def test_build_and_read_markdown_archive_round_trip_manifest_and_documents() -> None:
    api = _tdoc_zip_module()
    model = _document_model()
    ftp_url = "tsg_ran/wg5/example.zip"
    documents = (
        _make_document(
            api,
            model,
            ftp_url=ftp_url,
            source_file="z/main.docx",
            file_order=0,
            markdown_text="# Main\n",
        ),
        _make_document(
            api,
            model,
            ftp_url=ftp_url,
            source_file="a/shared.docx",
            file_order=1,
            markdown_text="# First support\n",
        ),
        _make_document(
            api,
            model,
            ftp_url=ftp_url,
            source_file="b/shared.docx",
            file_order=2,
            markdown_text="# Second support\n",
        ),
    )

    raw = api.build_markdown_archive(
        documents,
        ftp_url=ftp_url,
        main_source_file="z/main.docx",
    )

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
    assert names == ["manifest.json", *(document.markdown_file for document in documents)]
    assert manifest["version"] == 1
    assert manifest["ftp_url"] == ftp_url
    assert manifest["main_source_file"] == "z/main.docx"
    assert [entry["source_file"] for entry in manifest["documents"]] == [
        "z/main.docx",
        "a/shared.docx",
        "b/shared.docx",
    ]

    decoded = api.read_markdown_archive(raw)

    assert decoded.legacy_markdown is None
    assert decoded.ftp_url == ftp_url
    assert decoded.main_source_file == "z/main.docx"
    assert tuple(document.source_file for document in decoded.documents) == tuple(
        document.source_file for document in documents
    )
    assert tuple(document.markdown_text for document in decoded.documents) == (
        "# Main\n",
        "# First support\n",
        "# Second support\n",
    )
    assert decoded.documents[1].markdown_file != decoded.documents[2].markdown_file


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"legacy markdown", "legacy markdown"),
        (gzip.compress(b"gzip markdown", mtime=0), "gzip markdown"),
        (_make_zip([("old-name.md", b"single zip markdown")]), "single zip markdown"),
    ],
)
def test_read_markdown_archive_preserves_legacy_decoding(raw: bytes, expected: str) -> None:
    api = _tdoc_zip_module()

    decoded = api.read_markdown_archive(raw)

    assert decoded.legacy_markdown == expected
    assert decoded.documents == ()


def test_make_zip_bytes_do_not_depend_on_the_wall_clock(monkeypatch) -> None:
    outputs = []
    for now in (1_700_000_000.0, 1_800_000_000.0):
        monkeypatch.setattr(zipfile.time, "time", lambda now=now: now)
        outputs.append(_make_zip([("old-name.md", b"single zip markdown")]))

    assert outputs[0] == outputs[1]


def test_read_markdown_archive_never_returns_manifest_as_markdown() -> None:
    api = _tdoc_zip_module()
    model = _document_model()
    ftp_url = "tsg_ran/wg5/example.zip"
    document = _make_document(
        api,
        model,
        ftp_url=ftp_url,
        source_file="main.docx",
        file_order=0,
        markdown_text="# Actual Markdown\n",
    )
    raw = api.build_markdown_archive(
        (document,),
        ftp_url=ftp_url,
        main_source_file="main.docx",
    )

    decoded = api.read_markdown_archive(raw)

    assert decoded.legacy_markdown is None
    assert tuple(document.markdown_text for document in decoded.documents) == (
        "# Actual Markdown\n",
    )


@pytest.mark.parametrize(
    "manifest_version",
    [pytest.param(True, id="bool"), pytest.param(1.0, id="float")],
)
def test_markdown_archive_rejects_non_integer_manifest_version(
    manifest_version: object,
) -> None:
    api = _tdoc_zip_module()
    model = _document_model()
    ftp_url = "tsg_ran/wg5/archive.zip"
    document = _make_document(
        api,
        model,
        ftp_url=ftp_url,
        source_file="main.docx",
        file_order=0,
        markdown_text="main body",
    )

    with pytest.raises(ValueError, match="manifest version"):
        model.MarkdownArchive(
            ftp_url=ftp_url,
            main_source_file="main.docx",
            documents=(document,),
            manifest_version=manifest_version,
        )


def test_tdoc_parse_bundle_accepts_deterministic_casefold_path_order() -> None:
    api = _tdoc_zip_module()
    model = _document_model()
    ftp_url = "tsg_ran/wg5/example.zip"
    documents = (
        _make_document(
            api,
            model,
            ftp_url=ftp_url,
            source_file="z/main.docx",
            file_order=0,
            markdown_text="# Main\n",
        ),
        _make_document(
            api,
            model,
            ftp_url=ftp_url,
            source_file="a/support.docx",
            file_order=1,
            markdown_text="# Lowercase support\n",
        ),
        _make_document(
            api,
            model,
            ftp_url=ftp_url,
            source_file="B/support.docx",
            file_order=2,
            markdown_text="# Uppercase support\n",
        ),
    )
    cr_model = importlib.import_module("doc3gpp.models.tdoc_cr")
    main_result = cr_model.TDocCRParseResult(
        cover=cr_model.TDocCRDetails(tdoc_id="R5s260009")
    )

    bundle = model.TDocParseBundle(
        documents=documents,
        main_source_file="z/main.docx",
        main_parse_result=main_result,
    )

    assert tuple(document.source_file for document in bundle.documents) == (
        "z/main.docx",
        "a/support.docx",
        "B/support.docx",
    )


def test_markdown_archive_round_trips_casefold_path_order() -> None:
    api = _tdoc_zip_module()
    model = _document_model()
    ftp_url = "tsg_ran/wg5/example.zip"
    documents = (
        _make_document(
            api,
            model,
            ftp_url=ftp_url,
            source_file="z/main.docx",
            file_order=0,
            markdown_text="# Main\n",
        ),
        _make_document(
            api,
            model,
            ftp_url=ftp_url,
            source_file="a/support.docx",
            file_order=1,
            markdown_text="# Lowercase support\n",
        ),
        _make_document(
            api,
            model,
            ftp_url=ftp_url,
            source_file="B/support.docx",
            file_order=2,
            markdown_text="# Uppercase support\n",
        ),
    )
    raw = api.build_markdown_archive(
        documents,
        ftp_url=ftp_url,
        main_source_file="z/main.docx",
    )

    decoded = api.read_markdown_archive(raw)

    assert tuple(document.source_file for document in decoded.documents) == (
        "z/main.docx",
        "a/support.docx",
        "B/support.docx",
    )
