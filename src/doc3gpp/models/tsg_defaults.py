"""Canonical TSG reference definitions and group-page URL composition."""

from __future__ import annotations

import re
from typing import Final

from doc3gpp.models.tsg import Tsg

_TSG_URL_BASE: Final[str] = "https://www.3gpp.org/3gpp-groups"

_FAMILY_PATHS: Final[dict[str, str]] = {
    "RAN": "radio-access-networks-ran",
    "SA": "service-system-aspects-sa",
    "CT": "core-network-terminals-ct",
}

_TSG_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<family>RAN|SA|CT)\s+(?P<kind>WG|AH)(?P<num>\d+)$"
)
_TSG_PLENARY_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<family>RAN|SA|CT)\s+Plenary$"
)

_DEFAULT_TSGS: Final[tuple[Tsg, ...]] = (
    Tsg(
        tsg_name="RAN Plenary",
        short_name="RP",
        description="Radio Access Network Plenary",
    ),
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
        tsg_name="RAN WG6",
        short_name="R6",
        description=(
            "(CLOSED) GSM/EDGE Radio Access Network (GERAN) and UMTS Radio Access "
            "(UTRA)"
        ),
    ),
    Tsg(
        tsg_name="RAN AH1",
        short_name="RT",
        description="ITU-R Ad Hoc",
    ),
    Tsg(
        tsg_name="SA Plenary",
        short_name="SP",
        description="Service and System Aspects Plenary",
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
        tsg_name="CT Plenary",
        short_name="CP",
        description="Core Network and Terminals Plenary",
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

_TSGS_WITHOUT_URL: Final[frozenset[str]] = frozenset({"R6"})


def build_tsg_url(tsg_name: str) -> str | None:
    """Compose a 3GPP group page URL for a recognized TSG name."""
    name = tsg_name.strip()
    match = _TSG_NAME_RE.match(name)
    if match is not None:
        family = match.group("family")
        kind = match.group("kind").lower()
        number = match.group("num")
        family_path = _FAMILY_PATHS.get(family)
        if family_path is None:
            return None
        return f"{_TSG_URL_BASE}/{family_path}/{family.lower()}-{kind}{number}"

    plenary_match = _TSG_PLENARY_NAME_RE.match(name)
    if plenary_match is None:
        return None
    family = plenary_match.group("family")
    family_path = _FAMILY_PATHS.get(family)
    if family_path is None:
        return None
    return f"{_TSG_URL_BASE}/{family_path}/"


def default_tsgs() -> tuple[Tsg, ...]:
    """Return canonical TSG records with their current URL defaults composed."""
    return tuple(
        Tsg(
            tsg_name=tsg.tsg_name,
            short_name=tsg.short_name,
            description=tsg.description,
            url=(
                None
                if tsg.short_name in _TSGS_WITHOUT_URL
                else build_tsg_url(tsg.tsg_name)
            ),
        )
        for tsg in _DEFAULT_TSGS
    )


__all__ = ["build_tsg_url", "default_tsgs"]
