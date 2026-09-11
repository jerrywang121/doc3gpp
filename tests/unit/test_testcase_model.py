from doc3gpp.models.testcase import (
    TestCase, TestCaseDetail, TestCaseSource, TestCaseStatus,
    TestCaseWithStatuses, TestcaseSourceNotFoundError,
    TestcaseWorkbookNotFoundError,
)

def test_dataclass_shapes():
    tc = TestCase(testcase_id="TC_1", title="T", group="5G", spec="38.523-1")
    assert tc.title == "T"
    st = TestCaseStatus(testcase_id="TC_1", path="FR1", gcf_ptcrb="Approved", ttcn_status="Approved")
    assert st.path == "FR1"
    assert TestCaseWithStatuses(testcase=tc, statuses={"FR1": "Approved"}).statuses == {"FR1": "Approved"}
    assert isinstance(TestCaseDetail(testcase=tc, statuses=[st]).statuses, list)
    src = TestCaseSource(filename="f.zip", year=2024, week=32, revision=0)
    assert src.parsed_at is None
    assert issubclass(TestcaseSourceNotFoundError, LookupError)
    assert issubclass(TestcaseWorkbookNotFoundError, ValueError)
