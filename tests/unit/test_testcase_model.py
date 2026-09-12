from doc3gpp.models.testcase import (
    TestCase, TestCaseDetail, TestCaseSource, TestCaseStatus,
    TestCaseWithStatuses, TestcaseSourceNotFoundError,
    TestcaseWorkbookNotFoundError,
)

def test_dataclass_shapes():
    tc = TestCase(testcase_id="TC_1", group="5G", title="T", spec="38.523-1")
    assert tc.title == "T"
    st = TestCaseStatus(testcase_id="TC_1", group="5G", path="FR1", gcf_ptcrb="Approved", ttcn_status="Approved")
    assert st.path == "FR1"
    rows = [TestCaseStatus(testcase_id="TC_1", group="5G", path="FR1", gcf_ptcrb="Approved", ttcn_status="Approved")]
    assert TestCaseWithStatuses(testcase=tc, statuses=rows).statuses == rows
    assert isinstance(TestCaseDetail(testcase=tc, statuses=[st]).statuses, list)
    src = TestCaseSource(filename="f.zip", year=2024, week=32, revision=0)
    assert src.parsed_at is None
    assert issubclass(TestcaseSourceNotFoundError, LookupError)
    assert issubclass(TestcaseWorkbookNotFoundError, ValueError)


def test_with_statuses_holds_status_rows():
    from doc3gpp.models.testcase import TestCaseStatus
    tc = TestCase(testcase_id="TC_1", group="5G", title="T", spec="38.523-1")
    rows = [
        TestCaseStatus(testcase_id="TC_1", group="5G", path="FR1", gcf_ptcrb="Approved", ttcn_status="Approved"),
        TestCaseStatus(testcase_id="TC_1", group="5G", path="FR2", gcf_ptcrb=None, ttcn_status=None),
    ]
    assert TestCaseWithStatuses(testcase=tc, statuses=rows).statuses == rows
