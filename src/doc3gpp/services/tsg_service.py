"""Service layer for 3GPP TSG (Technical Specification Group) reference data.

Holds the canonical TSG list and provides seeding, validation, and query
operations against the configured :class:`TsgRepository`.
"""

from __future__ import annotations

import logging
import re
from typing import Final

from doc3gpp.models.tsg import Tsg
from doc3gpp.repository.protocols import TsgRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL pattern composition
# ---------------------------------------------------------------------------
#
# The 3GPP group page URL is derived from the TSG family and number using the
# pattern documented for this project:
#
#   RAN WG{#}:  https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-wg{#}
#   SA  WG{#}:  https://www.3gpp.org/3gpp-groups/service-system-aspects-sa/sa-wg{#}
#   CT  WG{#}:  https://www.3gpp.org/3gpp-groups/core-network-terminals-ct/ct-wg{#}
#   RAN AH1:    https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-ah1
#
_TSG_URL_BASE: Final[str] = "https://www.3gpp.org/3gpp-groups"

# Mapping from the family segment of ``tsg_name`` (uppercase) to its URL path.
# Unknown families fall back to ``None`` so callers can still seed a TSG whose
# URL has not been mapped yet.
_FAMILY_PATHS: Final[dict[str, str]] = {
    "RAN": "radio-access-networks-ran",
    "SA": "service-system-aspects-sa",
    "CT": "core-network-terminals-ct",
}

# ``tsg_name`` shape: ``"<FAMILY> <KIND><#>"`` where KIND is "WG" or "AH".
_TSG_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<family>RAN|SA|CT)\s+(?P<kind>WG|AH)(?P<num>\d+)$")


def build_tsg_url(tsg_name: str) -> str | None:
    """Compose a 3GPP group page URL for ``tsg_name`` (e.g. ``"RAN WG1"``).

    Returns ``None`` if the family is not recognised, so callers can still
    persist a TSG record without a URL.
    """
    match = _TSG_NAME_RE.match(tsg_name.strip())
    if not match:
        return None

    family = match.group("family")
    kind = match.group("kind").lower()
    num = match.group("num")
    family_path = _FAMILY_PATHS.get(family)
    if family_path is None:
        return None

    return f"{_TSG_URL_BASE}/{family_path}/{family.lower()}-{kind}{num}"


# ---------------------------------------------------------------------------
# Canonical seed data
# ---------------------------------------------------------------------------
#
# Mirrors the CSV published by 3GPP. Short names are stored in their canonical
# uppercase form (e.g. ``"R1"``) and matched case-insensitively at the
# repository layer so user input such as ``--tsg r5`` resolves correctly.
_DEFAULT_TSGS: Final[tuple[Tsg, ...]] = (
    Tsg(
        tsg_name="RAN WG1",
        short_name="R1",
        description="Radio Layer 1 (Physical layer)",
    ),
    Tsg(
        tsg_name="RAN WG2",
        short_name="R2",
        description="Radio layer 2 and Radio layer 3 Radio Resource Control",
    ),
    Tsg(
        tsg_name="RAN WG3",
        short_name="R3",
        description="UTRAN/E-UTRAN/NG-RAN architecture and related network interfaces",
    ),
    Tsg(
        tsg_name="RAN WG4",
        short_name="R4",
        description="Radio Performance and Protocol Aspects",
    ),
    Tsg(
        tsg_name="RAN WG5",
        short_name="R5",
        description="Mobile terminal conformance testing",
    ),
    Tsg(
        tsg_name="RAN AH1",
        short_name="RT",
        description="ITU-R Ad Hoc",
    ),
    Tsg(
        tsg_name="SA WG1",
        short_name="S1",
        description="Services",
    ),
    Tsg(
        tsg_name="SA WG2",
        short_name="S2",
        description="System Architecture and Services",
    ),
    Tsg(
        tsg_name="SA WG3",
        short_name="S3",
        description="Security and Privacy",
    ),
    Tsg(
        tsg_name="SA WG4",
        short_name="S4",
        description="Multimedia Codecs, Systems and Services",
    ),
    Tsg(
        tsg_name="SA WG5",
        short_name="S5",
        description="Management, Orchestration and Charging",
    ),
    Tsg(
        tsg_name="SA WG6",
        short_name="S6",
        description="Application Enablement and Critical Communication Applications",
    ),
    Tsg(
        tsg_name="CT WG1",
        short_name="C1",
        description="User Equipment to Core Network protocols",
    ),
    Tsg(
        tsg_name="CT WG3",
        short_name="C3",
        description=(
            "Network Capability Exposure, Policy and Charging Control, "
            "Artificial Intelligence, Interworking with External Networks"
        ),
    ),
    Tsg(
        tsg_name="CT WG4",
        short_name="C4",
        description="Core Network Protocols",
    ),
    Tsg(
        tsg_name="CT WG6",
        short_name="C6",
        description="Smart Card Application Aspects",
    ),
)


class TsgService:
    """Service for seeding and querying 3GPP TSG reference records."""

    def __init__(self, repository: TsgRepository) -> None:
        """Initialize the service with a repository backing the TSG storage."""
        self._repository = repository

    def list_all(self) -> list[Tsg]:
        """Return all stored TSG records ordered by ``tsg_name``."""
        return self._repository.list_all()

    def get_by_short_name(self, short_name: str) -> Tsg | None:
        """Return a stored TSG by its short name (case-insensitive)."""
        return self._repository.get_by_short_name(short_name)

    def get_by_tsg_name(self, tsg_name: str) -> Tsg | None:
        """Return a stored TSG by its full ``tsg_name`` (case-insensitive)."""
        return self._repository.get_by_tsg_name(tsg_name)

    def is_known_short_name(self, short_name: str) -> bool:
        """Return ``True`` iff ``short_name`` matches a stored TSG.

        The check is case-insensitive and treats an empty/blank input as
        unknown so callers can short-circuit validation.
        """
        if not short_name or not short_name.strip():
            return False
        return self._repository.get_by_short_name(short_name) is not None

    def known_short_names(self) -> list[str]:
        """Return the canonical uppercase short names of all stored TSGs."""
        return [tsg.short_name for tsg in self._repository.list_all()]

    def seed_defaults(self) -> int:
        """Insert or refresh the canonical 3GPP TSG list.

        Existing records are updated in place (matching by ``tsg_name``), so
        this method is safe to call repeatedly. URLs are composed from the
        project URL pattern when missing, ensuring every seed row carries a
        valid 3GPP group page link.
        """
        seed_rows: list[Tsg] = []
        for tsg in _DEFAULT_TSGS:
            url = tsg.url or build_tsg_url(tsg.tsg_name)
            seed_rows.append(
                Tsg(
                    tsg_name=tsg.tsg_name,
                    short_name=tsg.short_name,
                    description=tsg.description,
                    url=url,
                )
            )
        logger.info("Seeding %s default TSG records", len(seed_rows))
        return self._repository.upsert_many(seed_rows)


__all__ = ["TsgService", "build_tsg_url"]
