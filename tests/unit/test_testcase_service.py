# ruff: noqa: F401,E702
def test_skip_matrix():
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
        def replace_statuses(self, tid, rows): pass
    svc = TestCaseService(Repo())
    import doc3gpp.services.testcase_service as m
    m.list_history_files = lambda client=None: ["TTCN CR Agreement Status 2024-wk32.zip"]
    m.fetch_testcase_zip = lambda fn, client=None: b"ZIP"
    m.extract_workbook = lambda b: b"XLSX"
    m.parse_testcase_workbook = lambda b: ([], [], [])
    out1 = svc.sync()
    assert out1.status == "synced"
    out2 = svc.sync()
    assert out2.status == "skipped"
    out3 = svc.sync(force=True)
    assert out3.status == "synced"
