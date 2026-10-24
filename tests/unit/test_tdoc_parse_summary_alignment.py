"""Value-column alignment of the URL-folder batch summary."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.tdoc_cr import DirectParseBatchResult

SUMMARY_LABELS = ("Skipped:", "Newly parsed:", "Cache hits:", "Failures:")


class _FakeBatchService:
    def __init__(self, batch_result: DirectParseBatchResult) -> None:
        self.batch_result = batch_result

    def extract_from_url_batch(
        self,
        url: str,
        *,
        max_depth: int = 2,
        force: bool = False,
        max_tdoc_size_bytes: int = 0,
        on_progress=None,
        is_cancelled=None,
    ) -> DirectParseBatchResult:
        return self.batch_result


def _value_column(output: str, label: str) -> int:
    line = next(ln for ln in output.splitlines() if ln.startswith(label)).rstrip()
    return len(line) - len(line.split()[-1])


def test_url_folder_summary_value_lines_share_one_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = DirectParseBatchResult(
        results=[],
        failures={"https://www.3gpp.org/ftp/Docs/R5s260002.zip": "ValueError: bad"},
        skipped={
            "https://www.3gpp.org/ftp/Docs/R5s260004.zip": (
                "TDocTypeSkippedError: type 'other' is not parsed"
            ),
        },
    )
    fake = _FakeBatchService(batch)
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_cr_service", lambda *args, **kwargs: fake)

    result = CliRunner().invoke(
        app, ["tdoc", "parse", "--from-url", "https://www.3gpp.org/ftp/Docs/"]
    )

    assert "Skipped:" in result.output
    columns = {label: _value_column(result.output, label) for label in SUMMARY_LABELS}
    assert columns == dict.fromkeys(SUMMARY_LABELS, 33)
