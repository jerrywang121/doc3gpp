from __future__ import annotations

from pathlib import Path

from doc3gpp.models.tdoc import TDoc
from doc3gpp.storage.cache import FileCache
from doc3gpp.storage.export import export_tdocs_csv


def test_file_cache_path_for_normalizes_key(tmp_path) -> None:
    cache = FileCache(tmp_path / "cache")
    path = cache.path_for("tsg_ran/WG5_Test_ex-T1")
    assert path.name == "tsg_ran_WG5_Test_ex-T1.html"


def test_export_tdocs_csv_writes_rows(tmp_path) -> None:
    out = Path(tmp_path) / "tdocs.csv"
    records = [
        TDoc(tdoc_id="R1-000001", title="Title 1", meeting_name="RAN1#100", url="https://example.test/1"),
        TDoc(tdoc_id="R1-000002", title="Title 2"),
    ]

    export_tdocs_csv(out, records)
    text = out.read_text(encoding="utf-8")

    assert "tdoc_id,title,meeting,url" in text
    assert "R1-000001,Title 1,RAN1#100,https://example.test/1" in text
    assert "R1-000002,Title 2,," in text
