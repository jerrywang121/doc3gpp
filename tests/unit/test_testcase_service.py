# ruff: noqa: F401,E702
def test_skip_matrix(monkeypatch):
    from datetime import datetime, timezone
    from doc3gpp.models.testcase import TestCaseSource
    from doc3gpp.services.testcase_service import TestCaseService
    class Repo:
        def __init__(self): self.sources = {}; self.cases = []
        def get_source(self, fn): return self.sources.get(fn)
        def record_download(self, src): self.sources[src.filename] = src
        def record_parsed(self, fn, at, tc, st):
            s = self.sources[fn]; s.parsed_at = at; s.testcase_count = tc; s.status_count = st
        def upsert_many(self, cases): self.cases = cases; return len(cases)
        def replace_statuses(self, tid, group, rows): pass
    svc = TestCaseService(Repo())
    import doc3gpp.services.testcase_service as m
    monkeypatch.setattr(m, "list_history_files", lambda client=None: ["TTCN CR Agreement Status 2024-wk32.zip"])
    monkeypatch.setattr(m, "fetch_testcase_zip", lambda fn, client=None: b"ZIP")
    monkeypatch.setattr(m, "extract_workbook", lambda b: b"XLSX")
    monkeypatch.setattr(m, "parse_testcase_workbook", lambda b: ([], [], []))
    out1 = svc.sync()
    assert out1.status == "synced"
    out2 = svc.sync()
    assert out2.status == "skipped"
    out3 = svc.sync(force=True)
    assert out3.status == "synced"


def test_sync_zero_status_tc_gets_replace_with_empty(monkeypatch):
    from doc3gpp.models.testcase import TestCase
    from doc3gpp.services.testcase_service import TestCaseService
    class Repo:
        def __init__(self): self.replaced = {}
        def get_source(self, fn): return None
        def record_download(self, src): pass
        def record_parsed(self, fn, at, tc, st): pass
        def upsert_many(self, cases): return len(cases)
        def replace_statuses(self, tid, group, rows): self.replaced[(tid, group)] = list(rows)
    repo = Repo()
    svc = TestCaseService(repo)
    import doc3gpp.services.testcase_service as m
    header = TestCase(testcase_id="TC_EMPTY", title="No cells", group="5G")
    monkeypatch.setattr(m, "list_history_files", lambda client=None: ["TTCN CR Agreement Status 2024-wk32.zip"])
    monkeypatch.setattr(m, "fetch_testcase_zip", lambda fn, client=None: b"ZIP")
    monkeypatch.setattr(m, "extract_workbook", lambda b: b"XLSX")
    monkeypatch.setattr(m, "parse_testcase_workbook", lambda b: ([header], [], []))
    out = svc.sync()
    assert out.status == "synced"
    assert repo.replaced == {("TC_EMPTY", "5G"): []}
