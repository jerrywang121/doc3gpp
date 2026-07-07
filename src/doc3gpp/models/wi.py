"""Domain model for 3GPP Work Items (WIs)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Wi:
    """A 3GPP Work Item (WI) record scraped from the DynaReport WIs page.

    The 3GPP work-item tracker exposes one DynaReport page per TSG that lists
    every active WI where the TSG is the sole responsible group. Each row in
    that table becomes a :class:`Wi`.

    Attributes:
        wi_id: Canonical numeric WI identifier from the 3GPP portal
            (``workitemId`` URL parameter, e.g. ``1031076``).
        acronym: Short symbolic identifier for the WI
            (e.g. ``LTE_TN_NR_NTN_mob-Core``).
        release: 3GPP release marker (e.g. ``Rel-19``).
        name: Full human-readable WI title as displayed on the 3GPP WIs page.
        tsg_short: Uppercase TSG short name that owns this WI (e.g. ``R5``).
            Foreign key to ``tsgs.short_name``.
    """

    wi_id: int
    acronym: str
    release: str
    name: str
    tsg_short: str