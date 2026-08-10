from doc3gpp.models.tsg import Tsg
from doc3gpp.services.tsg_service import TsgService


class _FakeTsgRepository:
    """In-memory TsgRepository double used by the service unit tests."""

    def __init__(self) -> None:
        self.rows: list[Tsg] = []

    def upsert_many(self, tsgs: list[Tsg]) -> int:
        for t in tsgs:
            existing = next(
                (r for r in self.rows if r.tsg_name.lower() == t.tsg_name.lower()), None
            )
            if existing is not None:
                existing.tsg_name = t.tsg_name
                existing.short_name = t.short_name
                existing.description = t.description
                existing.url = t.url
            else:
                self.rows.append(t)
        return len(tsgs)

    def list_all(self) -> list[Tsg]:
        return sorted(self.rows, key=lambda r: r.tsg_name)

    def get_by_short_name(self, short_name: str) -> Tsg | None:
        return next(
            (r for r in self.rows if r.short_name.lower() == short_name.lower()),
            None,
        )

    def get_by_tsg_name(self, tsg_name: str) -> Tsg | None:
        return next(
            (r for r in self.rows if r.tsg_name.lower() == tsg_name.lower()), None
        )

    def count(self) -> int:
        return len(self.rows)

    def update_spec_last_sync(self, short_name: str, synced_at) -> bool:
        row = self.get_by_short_name(short_name)
        if row is None:
            return False
        row.spec_last_sync = synced_at
        return True


def test_seed_defaults_populates_all_nineteen() -> None:
    repo = _FakeTsgRepository()
    service = TsgService(repo)  # type: ignore[arg-type]

    seeded = service.seed_defaults()
    assert seeded == 19
    assert repo.count() == 19
    short_names = {t.short_name for t in service.list_all()}
    assert short_names == {"R1", "R2", "R3", "R4", "R5", "RT", "S1", "S2", "S3",
                           "S4", "S5", "S6", "C1", "C3", "C4", "C6",
                           "RP", "SP", "CP"}


def test_seed_defaults_assigns_urls_from_pattern() -> None:
    repo = _FakeTsgRepository()
    service = TsgService(repo)  # type: ignore[arg-type]

    service.seed_defaults()
    by_name = {t.tsg_name: t for t in service.list_all()}

    assert (
        by_name["RAN WG1"].url
        == "https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-wg1"
    )
    assert (
        by_name["SA WG3"].url
        == "https://www.3gpp.org/3gpp-groups/service-system-aspects-sa/sa-wg3"
    )
    assert (
        by_name["CT WG6"].url
        == "https://www.3gpp.org/3gpp-groups/core-network-terminals-ct/ct-wg6"
    )
    assert (
        by_name["RAN AH1"].url
        == "https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-ah1"
    )


def test_seed_defaults_assigns_plenary_urls_from_pattern() -> None:
    """Plenary TSGs get a family-root URL with a trailing slash (no subgroup
    suffix), unlike WG/AH URLs which end without a slash.
    """
    repo = _FakeTsgRepository()
    service = TsgService(repo)  # type: ignore[arg-type]

    service.seed_defaults()
    by_name = {t.tsg_name: t for t in service.list_all()}

    ran = by_name["RAN Plenary"]
    assert ran.short_name == "RP"
    assert (
        ran.url
        == "https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/"
    )
    assert ran.url.endswith("/")

    sa = by_name["SA Plenary"]
    assert sa.short_name == "SP"
    assert (
        sa.url
        == "https://www.3gpp.org/3gpp-groups/service-system-aspects-sa/"
    )
    assert sa.url.endswith("/")

    ct = by_name["CT Plenary"]
    assert ct.short_name == "CP"
    assert (
        ct.url
        == "https://www.3gpp.org/3gpp-groups/core-network-terminals-ct/"
    )
    assert ct.url.endswith("/")


def test_seed_defaults_is_idempotent() -> None:
    repo = _FakeTsgRepository()
    service = TsgService(repo)  # type: ignore[arg-type]

    service.seed_defaults()
    service.seed_defaults()
    assert repo.count() == 19


def test_seed_defaults_refreshes_url_from_pattern() -> None:
    """``seed_defaults`` always recomposes the URL from the project pattern,
    even when the caller previously stored a different URL. This keeps the
    seed authoritative for canonical reference data.
    """
    repo = _FakeTsgRepository()
    repo.rows.append(
        Tsg(
            tsg_name="RAN WG1",
            short_name="R1",
            description="Custom description",
            url="https://custom.example/r1",
        )
    )
    service = TsgService(repo)  # type: ignore[arg-type]

    service.seed_defaults()
    refreshed = service.get_by_short_name("R1")
    assert refreshed is not None
    assert refreshed.url == (
        "https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-wg1"
    )


def test_is_known_short_name_case_insensitive() -> None:
    repo = _FakeTsgRepository()
    repo.rows.append(Tsg(tsg_name="RAN WG5", short_name="R5", description="x", url=None))
    service = TsgService(repo)  # type: ignore[arg-type]

    assert service.is_known_short_name("R5")
    assert service.is_known_short_name("r5")
    assert not service.is_known_short_name("R99")
    assert not service.is_known_short_name("")
    assert not service.is_known_short_name("   ")


def test_known_short_names_returns_canonical_uppercase() -> None:
    repo = _FakeTsgRepository()
    repo.rows.append(Tsg(tsg_name="SA WG2", short_name="S2", description="x", url=None))
    service = TsgService(repo)  # type: ignore[arg-type]

    names = service.known_short_names()
    assert names == ["S2"]


def test_get_by_short_name_or_tsg_name() -> None:
    repo = _FakeTsgRepository()
    repo.rows.append(
        Tsg(tsg_name="CT WG1", short_name="C1", description="UE-CN", url=None)
    )
    service = TsgService(repo)  # type: ignore[arg-type]

    assert service.get_by_short_name("c1") is not None
    assert service.get_by_tsg_name("ct wg1") is not None
    assert service.get_by_short_name("missing") is None
    assert service.get_by_tsg_name("missing") is None
