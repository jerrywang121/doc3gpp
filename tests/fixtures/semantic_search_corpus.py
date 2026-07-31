"""Shared fixture corpus for the semantic (embedding + vector) search tests.

8 rows spanning the variety of shapes the semantic stack needs to
handle:

* 3 with TTCN sidecars (``tdoc_cr_ttcn_details`` rows)
* 2 with change-details rows (``tdoc_cr_change_details``)
* 1 metadata-only row (no cover / ttcn / change — LS, DRAFT parity)
* 1 multi-chunk row (long cover text → 5 chunks)
* 1 single-chunk row (short cover text → 1 chunk)
* ≥1 with a spec-number reference (``38.300``)
* ≥1 with hyphenated jargon (``NB-IoT``)

Embeddings are computed via :func:`_feature_embedding` — a
deterministic bag-of-tokens model that projects a fixed vocabulary
into a 384-dim float32 vector. Each text contributes ``+1.0`` at every
position corresponding to a vocabulary token it contains; the result
is L2-normalised. Two texts that share the NB-IoT triplet
(``nb-iot``, ``power``, ``saving``) produce vectors that are
near-identical (cosine similarity ≈ 1) along those dimensions, while
unrelated texts sit near orthogonal positions. This gives
``search sem`` a deterministic NB-IoT winner without loading a
sentence-transformers model.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import numpy as np


EMBEDDING_DIM = 384


_VOCAB: list[str] = [
    "nb-iot",
    "power",
    "saving",
    "prach",
    "coverage",
    "enhancement",
    "nbiot",
    "lte",
    "nr",
    "mac",
    "rlc",
    "pdcp",
    "rrc",
    "phy",
    "sidelink",
    "unlicensed",
    "ue",
    "enb",
    "gnb",
    "cr",
    "tdoc",
    "spec",
    "rel-17",
    "rel-18",
    "rel-16",
    "rel-15",
    "ttcn",
    "test",
    "case",
    "change",
    "marked",
    "issue",
    "resolution",
    "ls",
    "coordination",
    "agenda",
    "plenary",
    "session",
    "ran",
    "ran1",
    "ran2",
    "ran3",
    "ran4",
    "ran5",
    "sa",
    "sa2",
    "38.300",
    "38.321",
    "38.211",
    "38.212",
    "38.213",
    "38.323",
    "36.523",
    "37.901",
    "23.501",
    "23.502",
    "24.501",
    "27.007",
]


_VOCAB_INDEX = {tok: i for i, tok in enumerate(_VOCAB)}


def _tokenize(text: str) -> set[str]:
    out: set[str] = set()
    for raw in text.lower().replace("-", " ").replace(".", " ").split():
        cleaned = raw.strip(",;:()[]{}'\"")
        if cleaned:
            out.add(cleaned)
    return out


def _feature_embedding(text: str) -> np.ndarray:
    """Bag-of-vocab projection → L2-normalised 384-dim float32 vector."""
    vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    if not text:
        return vec
    for tok in _tokenize(text):
        idx = _VOCAB_INDEX.get(tok)
        if idx is not None:
            vec[idx] += 1.0
    n = float(np.linalg.norm(vec))
    if n > 0:
        vec = vec / n
    return vec


def _gz(payload: Any) -> bytes:
    return gzip.compress(json.dumps(payload).encode("utf-8"), compresslevel=9)


# Tuple shape: (tdoc_id, title, ftp_url_suffix, release, spec, tsg,
#               meeting_id, kinds).
# ``kinds`` is a set tagging which "shape" the row exemplifies; tests
# assert against specific names in the integration suite.
ROWS: list[tuple[str, str, str, str, str | None, str, int, set[str]]] = [
    (
        "SEM-NB-001", "NB-IoT power saving CRs for Rel-17 spec 38.300",
        "v1", "Rel-17", "38.300", "RAN", 9100, {"ttcn"},
    ),
    (
        "SEM-NB-002", "NB-IoT PRACH coverage enhancement Rel-17",
        "v2", "Rel-17", "38.321", "RAN1", 9101, {"ttcn"},
    ),
    (
        "SEM-TTCN-001", "TTCN test case for 5G NR PHY procedures spec 36.523",
        "v3", "Rel-17", "36.523", "RAN5", 9102, {"ttcn"},
    ),
    (
        "SEM-CHG-001", "Change-marked CR for NR sidelink Rel-17 spec 38.300",
        "v4", "Rel-17", "38.300", "RAN1", 9101, {"chg"},
    ),
    (
        "SEM-CHG-002", "Change-marked CR for NR PHY Rel-17 spec 38.211",
        "v5", "Rel-17", "38.211", "RAN1", 9101, {"chg"},
    ),
    (
        "SEM-LS-001", "LS coordination message RAN plenary",
        "v6", "Rel-17", None, "RAN", 9100, {"meta"},
    ),
    (
        "SEM-MULTI-001",
        "Rel-17 NR sidelink PHY MAC PDCP RLC test spec "
        "lte nr sidelink unlicensed coverage enhancement prach",
        "v7", "Rel-17", "37.901", "RAN1", 9101, {"multi"},
    ),
    (
        "SEM-MAC-001", "NR MAC procedures Rel-17 spec 38.321",
        "v8", "Rel-17", "38.321", "RAN1", 9101, {"single"},
    ),
]


TSGS: list[tuple[str, str, str]] = [
    ("RAN", "TSG RAN", "Radio Access Network"),
    ("SA", "TSG SA", "System Aspects"),
    ("RAN1", "TSG RAN WG1", "RAN WG1"),
    ("SA2", "TSG SA WG2", "SA WG2"),
    ("RAN5", "TSG RAN WG5", "RAN WG5"),
]


MEETINGS: list[tuple[int, str, str, str, str, str, str]] = [
    (9100, "SEM#9100", "SEM#9100 plenary", "Online", "RAN", "2026-03-01", "2026-03-05"),
    (9101, "SEM#9101", "SEM#9101 WG meeting", "Online", "RAN1", "2026-03-01", "2026-03-05"),
    (9102, "SEM#9102", "SEM#9102 WG meeting", "Online", "RAN5", "2026-03-01", "2026-03-05"),
]


# The NB-IoT query vector — used by both the embedder mock and the
# vector-search test to drive the rank-0/1 assertion.
NB_IOT_QUERY = "what CRs touch NB-IoT power saving"


def _compose_embed_text(
    tid: str, title: str, kinds: set[str],
) -> str:
    parts: list[str] = [title]
    parts.append(f"meeting {tid} context — RAN WG")
    if "meta" not in kinds:
        if "ttcn" in kinds:
            parts.append(
                "NB-IoT power saving issues and resolutions from "
                "the CR review for Rel-17 spec 38.300"
            )
        else:
            parts.append(
                "Change marked issue list for NR PHY procedures"
            )
    return " :: ".join(parts)


def _compute_chunks(embed_text: str, kinds: set[str]) -> list[str]:
    from doc3gpp.services.embedding.chunker import _chunks

    chunk_size = 8 if "multi" in kinds else 800
    chunks = _chunks(embed_text, size=chunk_size, overlap=2)
    if not chunks:
        chunks = [embed_text]
    if "multi" in kinds and len(chunks) > 5:
        chunks = chunks[:5]
    return chunks


PRECOMPUTED_EMBEDDINGS: dict[str, list[np.ndarray]] = {
    tid: [_feature_embedding(c) for c in _compute_chunks(
        _compose_embed_text(tid, title, kinds), kinds,
    )]
    for (tid, title, _suffix, _release, _spec, _tsg, _mid, kinds) in ROWS
}


ENCODE_TABLE: dict[str, np.ndarray] = {
    c: _feature_embedding(c)
    for (_tid, title, *_rest, kinds) in ROWS
    for c in _compute_chunks(_compose_embed_text(_tid, title, kinds), kinds)
}
ENCODE_TABLE[NB_IOT_QUERY] = _feature_embedding(NB_IOT_QUERY)


def build_semantic_corpus(engine: Any) -> list[str]:
    """Populate ``engine`` with the semantic-corpus rows.

    Returns the list of ``tdoc_id`` strings (in insertion order) so
    callers can drive downstream steps (eager
    :class:`SQLAlchemySearchIndexRepository.upsert` and
    :class:`SQLAlchemyVectorIndexRepository.upsert_chunks`) without
    re-querying the table.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        for short_name, tsg_name, description in TSGS:
            conn.execute(
                text(
                    "INSERT INTO tsgs (tsg_name, short_name, description) "
                    "VALUES (:tsg_name, :short_name, :description)"
                ),
                {
                    "tsg_name": tsg_name,
                    "short_name": short_name,
                    "description": description,
                },
            )

        for mid, name, title, location, tsg, start_date, end_date in MEETINGS:
            conn.execute(
                text(
                    """
                    INSERT INTO meetings (
                        meeting_id, name, title, location, tsg,
                        start_date, end_date, ftp_url, tdoc_list_last_sync
                    ) VALUES (
                        :mid, :name, :title, :location, :tsg,
                        :start_date, :end_date, :ftp_url,
                        :tdoc_list_last_sync
                    )
                    """
                ),
                {
                    "mid": mid,
                    "name": name,
                    "title": title,
                    "location": location,
                    "tsg": tsg,
                    "start_date": start_date,
                    "end_date": end_date,
                    "ftp_url": f"https://x/{name}",
                    "tdoc_list_last_sync": "2026-03-05T00:00:00",
                },
            )

        for (tid, title, suffix, release, spec, _tsg, mid, kinds) in ROWS:
            ftp = f"https://x/{tid}-{suffix}.zip"
            conn.execute(
                text(
                    """
                    INSERT INTO tdocs (
                        tdoc_id, meeting_id, title, ftp_url, type, source,
                        uploaded_date, release, spec
                    ) VALUES (
                        :tid, :mid, :title, :ftp, 'CR', 'TSG',
                        '2026-03-02', :release, :spec
                    )
                    """
                ),
                {
                    "tid": tid,
                    "mid": mid,
                    "title": title,
                    "ftp": ftp,
                    "release": release,
                    "spec": spec,
                },
            )

            if "ttcn" in kinds:
                conn.execute(
                    text(
                        """
                        INSERT INTO tdoc_cr_ttcn_details (
                            tdoc_id, ftp_url, testcase, ttcn_release,
                            required_changes, changed_functions
                        ) VALUES (
                            :tid, :ftp, :testcase, :ttcn_release,
                            :required_changes, :changed_functions
                        )
                        """
                    ),
                    {
                        "tid": tid,
                        "ftp": ftp,
                        "testcase": f"TC_{tid.replace('-', '_')}",
                        "ttcn_release": release,
                        "required_changes": _gz(
                            [{"file": "NB_IoT_mac.ttcn", "lines": "10-20"}]
                        ),
                        "changed_functions": "NB_IoT_mac.run",
                    },
                )

            if "chg" in kinds:
                conn.execute(
                    text(
                        """
                        INSERT INTO tdoc_cr_change_details (
                            tdoc_id, ftp_url, clauses, changes
                        ) VALUES (
                            :tid, :ftp, :clauses, :changes
                        )
                        """
                    ),
                    {
                        "tid": tid,
                        "ftp": ftp,
                        "clauses": "\n".join(["5.4.2.1", "5.4.2.2"]),
                        "changes": _gz(
                            [{"type": "ins", "text": "new NB-IoT MAC procedure"}]
                        ),
                    },
                )

    return [row[0] for row in ROWS]
