from doc3gpp.parsers.spec_release import normalise_release, release_from_version


def test_normalise_release_forms() -> None:
    assert normalise_release("Release 20") == "Rel-20"
    assert normalise_release("Release 9") == "Rel-9"
    assert normalise_release("R99") == "R99"
    assert normalise_release("Rel-17") == "Rel-17"
    assert normalise_release("draft") == "draft"
    assert normalise_release("pre-release") == "pre-release"
    assert normalise_release("") == ""
    assert normalise_release("   ") == ""


def test_release_from_version() -> None:
    assert release_from_version("0.2.1") == "draft"
    assert release_from_version("1.0.0") == "pre-release"
    assert release_from_version("2.3.0") == "pre-release"
    assert release_from_version("3.4.0") == "pre-release"
    assert release_from_version("18.3.0") == "Rel-18"
    assert release_from_version("4.0.0") == "Rel-4"
