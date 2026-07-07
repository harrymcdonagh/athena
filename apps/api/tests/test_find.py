import math

import pytest
from sqlalchemy import Engine

import apps.api.research.find as find_module
from apps.api.research.embeddings import EmbeddedChunk
from apps.api.research.find import (
    CANDIDATE_N,
    PASSAGES_PER_COMPANY,
    WIDE_SEARCH_LIMIT,
    FindResult,
    find_companies,
)
from apps.api.research.repository import ChunkMatch, Repository
from apps.api.tests.test_embeddings import (
    FakeEmbedder,
    QueryOnlyEmbedder,
    axis_vector,
    seed_filing,
    seed_second_company_filing,
)

# --- FIND mode: ADR-0011 §1 stage-1 retrieval, grouped by company ---


def seed_third_company_filing(db: Engine) -> int:
    return seed_filing(
        db,
        accession="0001652044-25-000001",
        ticker="GOOG",
        cik="0001652044",
        name="Alphabet Inc.",
        filing_url="https://sec.gov/goog-10k.htm",
    )


def blend_vector(axis0_weight: float) -> list[float]:
    """Unit vector whose cosine similarity to axis 0 is exactly axis0_weight."""
    vector = [0.0] * 1024
    vector[0] = axis0_weight
    vector[1] = math.sqrt(1.0 - axis0_weight**2)
    return vector


def seed_company_chunks(
    db: Engine,
    filing_id: int,
    chunks: list[tuple[str, list[float]]],
    source_url: str = "https://sec.gov/filing.htm",
) -> None:
    with db.begin() as conn:
        Repository(conn).replace_chunks(
            filing_id,
            "risk_factors",
            source_url,
            [EmbeddedChunk(text=text, embedding=vector) for text, vector in chunks],
            model="voyage-context-4",
            dimension=1024,
        )


def test_find_groups_matches_by_company_with_cited_passages(db: Engine) -> None:
    aapl = seed_filing(db)
    msft = seed_second_company_filing(db)
    seed_company_chunks(
        db,
        aapl,
        [("apple tariff exposure", axis_vector(0)), ("apple other topic", axis_vector(1))],
        source_url="https://sec.gov/aapl-10k.htm#risk",
    )
    seed_company_chunks(
        db,
        msft,
        [
            ("microsoft weaker mention", blend_vector(0.2)),
            ("microsoft tariff mention", blend_vector(0.6)),
        ],
        source_url="https://sec.gov/msft-10k.htm#risk",
    )

    result = find_companies(db, QueryOnlyEmbedder(axis_vector(0)), "tariff exposure")

    assert isinstance(result, FindResult)
    assert result.query == "tariff exposure"
    assert [m.ticker for m in result.matches] == ["AAPL", "MSFT"]
    assert [m.company_name for m in result.matches] == ["Apple Inc.", "Microsoft Corporation"]
    # passages are best-first within EVERY company, regardless of stored order
    assert [p.snippet for p in result.matches[0].passages] == [
        "apple tariff exposure",
        "apple other topic",
    ]
    assert [p.snippet for p in result.matches[1].passages] == [
        "microsoft tariff mention",
        "microsoft weaker mention",
    ]
    for match in result.matches:
        for passage in match.passages:
            assert passage.source_url.startswith("https://sec.gov/")


def test_find_orders_by_match_strength_not_by_company_attributes(db: Engine) -> None:
    """MSFT's text matches the query better than AAPL's, so MSFT orders first
    even though AAPL precedes it by id, ticker, and name. The ordering key is
    the best passage's cosine similarity — a retrieval fact about the text
    match, not any attribute of the company (ADR-0011 §5)."""
    aapl = seed_filing(db)
    msft = seed_second_company_filing(db)
    seed_company_chunks(db, aapl, [("apple weak mention", blend_vector(0.3))])
    seed_company_chunks(db, msft, [("microsoft strong mention", blend_vector(0.9))])

    result = find_companies(db, QueryOnlyEmbedder(axis_vector(0)), "tariffs")

    assert [m.ticker for m in result.matches] == ["MSFT", "AAPL"]
    assert result.matches[0].match_strength == pytest.approx(0.9, abs=1e-6)
    assert result.matches[1].match_strength == pytest.approx(0.3, abs=1e-6)
    # match_strength IS the best passage's similarity (1 - cosine distance)
    for match in result.matches:
        assert match.match_strength == pytest.approx(match.passages[0].similarity)


def test_find_candidate_cap_returns_only_top_n_companies(db: Engine) -> None:
    seed_company_chunks(db, seed_filing(db), [("apple", blend_vector(0.9))])
    seed_company_chunks(db, seed_second_company_filing(db), [("microsoft", blend_vector(0.8))])
    seed_company_chunks(db, seed_third_company_filing(db), [("alphabet", blend_vector(0.7))])

    result = find_companies(db, QueryOnlyEmbedder(axis_vector(0)), "tariffs", candidate_n=2)

    assert [m.ticker for m in result.matches] == ["AAPL", "MSFT"]  # the two strongest


def test_find_candidate_cap_is_a_ceiling_not_a_request_knob(
    db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0011 §3: the caps are structural. A caller can narrow candidate_n
    but never widen it past the module constant — widening means editing the
    named constant on purpose."""
    seed_company_chunks(db, seed_filing(db), [("apple", blend_vector(0.9))])
    seed_company_chunks(db, seed_second_company_filing(db), [("microsoft", blend_vector(0.8))])
    seed_company_chunks(db, seed_third_company_filing(db), [("alphabet", blend_vector(0.7))])
    monkeypatch.setattr(find_module, "CANDIDATE_N", 2)

    result = find_companies(db, QueryOnlyEmbedder(axis_vector(0)), "tariffs", candidate_n=999)

    assert len(result.matches) == 2


def test_find_passages_per_company_cap(db: Engine) -> None:
    aapl = seed_filing(db)
    seed_company_chunks(
        db,
        aapl,
        [(f"apple chunk {i}", blend_vector(0.9 - i * 0.1)) for i in range(5)],
    )

    result = find_companies(
        db, QueryOnlyEmbedder(axis_vector(0)), "tariffs", passages_per_company=2
    )

    (match,) = result.matches
    assert [p.snippet for p in match.passages] == ["apple chunk 0", "apple chunk 1"]  # best two
    similarities = [p.similarity for p in match.passages]
    assert similarities == sorted(similarities, reverse=True)


def test_find_reuses_the_existing_global_search_once_with_a_clamped_limit(
    db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0011 §2 stage 1 is ONE global vector search through the existing
    semantic_search path (no ticker filter), its limit clamped to the
    WIDE_SEARCH_LIMIT ceiling — never a new per-company search fan-out."""
    calls: list[dict[str, object]] = []

    def spy(
        engine: Engine,
        embedder: object,
        query: str,
        limit: int = 8,
        *,
        ticker: str | None = None,
        section: str | None = None,
    ) -> list[ChunkMatch]:
        calls.append({"query": query, "limit": limit, "ticker": ticker, "section": section})
        return []

    monkeypatch.setattr(find_module, "semantic_search", spy)

    find_companies(db, FakeEmbedder(), "tariffs", wide_search_limit=10_000)

    assert len(calls) == 1  # one wide search, no fan-out
    assert calls[0]["limit"] == WIDE_SEARCH_LIMIT  # 10_000 clamped to the ceiling
    assert calls[0]["ticker"] is None  # global: no company filter at stage 1


def test_find_no_matches_returns_empty_result_not_an_error(db: Engine) -> None:
    result = find_companies(db, QueryOnlyEmbedder(axis_vector(0)), "tariffs")
    assert result == FindResult(query="tariffs", matches=[])


def test_find_blank_query_raises(db: Engine) -> None:
    with pytest.raises(ValueError, match="query"):
        find_companies(db, FakeEmbedder(), "   ")


def test_find_rejects_non_positive_caps(db: Engine) -> None:
    """passages_per_company=0 would otherwise crash on passages[0] when
    computing match_strength; a zero candidate_n would silently return
    nothing. Both are caller errors, reported as such."""
    for kwargs in (
        {"wide_search_limit": 0},
        {"candidate_n": 0},
        {"passages_per_company": 0},
        {"passages_per_company": -1},
    ):
        with pytest.raises(ValueError, match="positive"):
            find_companies(db, FakeEmbedder(), "tariffs", **kwargs)


def test_find_default_caps_are_bounded() -> None:
    """The named defaults exist and are small by design (ADR-0011 §3):
    single digits to low tens, so a default FIND is bounded cost."""
    assert 1 <= CANDIDATE_N <= 25
    assert 1 <= PASSAGES_PER_COMPANY <= 10
    assert 1 <= WIDE_SEARCH_LIMIT <= 100
