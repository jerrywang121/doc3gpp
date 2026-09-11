from doc3gpp.scraping.testcase_source import (
    HISTORY_URL, parse_status_filename, select_latest,
)

def test_grammar_and_ordering():
    assert HISTORY_URL.endswith("/History/")
    assert parse_status_filename("TTCN CR Agreement Status 2019-wk15_rev1.zip") == (2019, 15, 1)
    assert parse_status_filename("TTCN CR Agreement Status 2024-wk32.zip") == (2024, 32, 0)
    assert parse_status_filename("random.zip") is None
    names = ["TTCN CR Agreement Status 2024-wk30.zip",
             "TTCN CR Agreement Status 2024-wk32.zip",
             "TTCN CR Agreement Status 2019-wk15_rev1.zip"]
    assert select_latest(names) == "TTCN CR Agreement Status 2024-wk32.zip"

def test_list_history_files_parses_anchors():
    from doc3gpp.scraping.testcase_source import list_history_files
    class Stub:
        def get_text(self, url): return (
            '<html><a href="TTCN%20CR%20Agreement%20Status%202024-wk32.zip">x</a>'
            '<a href="?C=N;O=D">sort</a><a href="other.zip">y</a></html>')
    assert list_history_files(Stub()) == ["TTCN CR Agreement Status 2024-wk32.zip"]
