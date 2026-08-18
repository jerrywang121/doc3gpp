"""Render ``LS_sample_r5_240001.md`` from the 3GPP LS template.

Run once to refresh the fixture; the rendered file is the canonical
fixture used by the unit + integration tests.
"""

from pathlib import Path

FIXTURE = """3GPP TSG RAN WG2 Meeting #104\tTDoc R5-240001

Title:\tLS on 5G_eHealth WI status update
Response to:\tLS R5-234567 on 5G_eHealth WI status from RAN WG3
Release:\tRelease 17
Work Item:\t5G_eHealth (WI-123456)

Source:\t3GPP TSG RAN WG2
To:\tRAN WG3, RAN WG4
Cc:\tSA WG2

Attachments:\tTR 38.901 v0.1.0 [draft].\tTS 38.300 v17.1.0.

1\tOverall description
…  (body omitted; the parser only inspects the header)

"""


def main() -> None:
    out = Path(__file__).parent / "LS_sample_r5_240001.md"
    out.write_text(FIXTURE, encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
