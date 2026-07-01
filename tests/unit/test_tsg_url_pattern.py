from doc3gpp.services.tsg_service import build_tsg_url


def test_build_tsg_url_ran_wg() -> None:
    assert (
        build_tsg_url("RAN WG1")
        == "https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-wg1"
    )
    assert (
        build_tsg_url("RAN WG5")
        == "https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-wg5"
    )


def test_build_tsg_url_sa_wg() -> None:
    assert (
        build_tsg_url("SA WG1")
        == "https://www.3gpp.org/3gpp-groups/service-system-aspects-sa/sa-wg1"
    )
    assert (
        build_tsg_url("SA WG6")
        == "https://www.3gpp.org/3gpp-groups/service-system-aspects-sa/sa-wg6"
    )


def test_build_tsg_url_ct_wg() -> None:
    assert (
        build_tsg_url("CT WG1")
        == "https://www.3gpp.org/3gpp-groups/core-network-terminals-ct/ct-wg1"
    )
    assert (
        build_tsg_url("CT WG6")
        == "https://www.3gpp.org/3gpp-groups/core-network-terminals-ct/ct-wg6"
    )


def test_build_tsg_url_ran_ah() -> None:
    assert (
        build_tsg_url("RAN AH1")
        == "https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-ah1"
    )


def test_build_tsg_url_unknown_returns_none() -> None:
    assert build_tsg_url("UNKNOWN WG1") is None
    assert build_tsg_url("") is None
    assert build_tsg_url("RAN WG") is None  # missing number
