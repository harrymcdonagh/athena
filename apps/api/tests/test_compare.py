import inspect
import math
from datetime import date

import pytest
from sqlalchemy import Engine, text

from apps.api.research.compare import (
    MAX_COMPARE_COMPANIES,
    PASSAGES_PER_COMPANY_COMPARE,
    QUALIFYING_SIMILARITY_FLOOR,
    ColumnClaim,
    ColumnDraft,
    CompareRefusal,
    MockColumnAnswerer,
    compare_companies,
    synthesize_column,
)
from apps.api.research.embeddings import EmbeddedChunk
from apps.api.research.repository import ChunkMatch, Repository
from apps.api.tests.test_embeddings import QueryOnlyEmbedder, axis_vector

# --- COMPARE mode: ADR-0012 — cited side-by-side over a named set, no ranking ---
#
# The answer model is MOCKED in this build (ADR-0012 mocked-build rhythm):
# these tests verify the structural properties from the ADR's Compliance
# section — the wall seam, refusal, pinning, entries prominence, coverage,
# and citation binding — with zero live answer-model calls.


def blend_vector(axis0_weight: float) -> list[float]:
    """Unit vector whose cosine similarity to axis 0 is exactly axis0_weight."""
    vector = [0.0] * 1024
    vector[0] = axis0_weight
    vector[1] = math.sqrt(1.0 - axis0_weight**2)
    return vector


def seed_reference(db: Engine, ticker: str, cik: str, name: str) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sec_ticker_reference (ticker, cik, company_name, exchange)"
                " VALUES (:ticker, :cik, :name, 'NASDAQ')"
            ),
            {"ticker": ticker, "cik": cik, "name": name},
        )


def seed_company_filing(
    db: Engine,
    *,
    ticker: str,
    cik: str,
    name: str,
    accession: str,
    form_type: str = "10-K",
    filing_date: str = "2025-10-31",
    period_end_date: str = "2025-09-27",
    filing_url: str = "https://sec.gov/filing.htm",
) -> int:
    with db.begin() as conn:
        company_id = conn.execute(
            text(
                "INSERT INTO companies (ticker, cik, name) VALUES (:ticker, :cik, :name)"
                " ON CONFLICT (cik) DO UPDATE SET name = EXCLUDED.name RETURNING id"
            ),
            {"ticker": ticker, "cik": cik, "name": name},
        ).scalar_one()
        filing_id: int = conn.execute(
            text(
                "INSERT INTO filings (company_id, accession_number, form_type, filing_date,"
                " period_end_date, filing_url, content_sha256)"
                " VALUES (:company_id, :acc, :form, :filed, :period, :url, 'abc123')"
                " RETURNING id"
            ),
            {
                "company_id": company_id,
                "acc": accession,
                "form": form_type,
                "filed": filing_date,
                "period": period_end_date,
                "url": filing_url,
            },
        ).scalar_one()
    return filing_id


def seed_chunks(
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
            [EmbeddedChunk(text=content, embedding=vector) for content, vector in chunks],
            model="voyage-context-4",
            dimension=1024,
        )


def seed_ready_company(
    db: Engine,
    ticker: str = "AAPL",
    cik: str = "0000320193",
    name: str = "Apple Inc.",
    accession: str = "0000320193-25-000001",
    source_url: str = "https://sec.gov/aapl-fy2025.htm",
) -> int:
    """Reference row + one 10-K + two qualifying chunks: a column-ready company."""
    seed_reference(db, ticker, cik, name)
    filing_id = seed_company_filing(
        db, ticker=ticker, cik=cik, name=name, accession=accession, filing_url=source_url
    )
    seed_chunks(
        db,
        filing_id,
        [(f"{ticker} tariff exposure", blend_vector(0.9)), (f"{ticker} more", blend_vector(0.5))],
        source_url=source_url,
    )
    return filing_id


class SpyAnswerer:
    """Records every draft_column input; delegates to the deterministic mock
    unless a canned draft is supplied."""

    def __init__(self, draft: ColumnDraft | None = None) -> None:
        self.calls: list[tuple[str, dict[str, ChunkMatch]]] = []
        self._draft = draft
        self._mock = MockColumnAnswerer()

    def draft_column(self, query: str, chunks: dict[str, ChunkMatch]) -> ColumnDraft:
        self.calls.append((query, chunks))
        if self._draft is not None:
            return self._draft
        return self._mock.draft_column(query, chunks)


class ExplodingEmbedder:
    """Proves no retrieval work happens: any embed call fails the test."""

    model = "voyage-context-4"
    dimension = 1024

    def embed_document(self, content: str) -> list[EmbeddedChunk]:
        raise AssertionError("embedding ran where none was allowed")

    def embed_query(self, content: str) -> list[float]:
        raise AssertionError("retrieval ran where none was allowed")


class ExplodingAnswerer:
    def draft_column(self, query: str, chunks: dict[str, ChunkMatch]) -> ColumnDraft:
        raise AssertionError("answer model ran where none was allowed")


def query_embedder() -> QueryOnlyEmbedder:
    return QueryOnlyEmbedder(axis_vector(0))


# --- the wall seam (ADR-0012 #6a): structural, not orchestration discipline ---


def test_seam_signature_admits_no_passage_parameter() -> None:
    """The synthesis seam takes (filing_id, query) and does its own scoped
    retrieval — there is NO parameter through which a passage list or a second
    company's evidence could enter (ADR-0012 #6a, Compliance). The signature
    IS the assertion, mirroring FIND's no-answerer-import."""
    params = set(inspect.signature(synthesize_column).parameters)
    assert params == {
        "engine",
        "embedder",
        "answerer",
        "filing_id",
        "query",
        "passages_per_company",
    }


def test_column_call_sees_only_the_pinned_filings_evidence(db: Engine) -> None:
    aapl_filing = seed_ready_company(db)
    msft_filing = seed_ready_company(
        db,
        ticker="MSFT",
        cik="0000789019",
        name="Microsoft Corporation",
        accession="0000789019-25-000001",
        source_url="https://sec.gov/msft-fy2025.htm",
    )
    spy = SpyAnswerer()

    compare_companies(db, query_embedder(), spy, ["AAPL", "MSFT"], "tariff exposure")

    assert len(spy.calls) == 2
    seen_filing_ids = []
    for _query, chunks in spy.calls:
        filing_ids = {match.filing_id for match in chunks.values()}
        assert len(filing_ids) == 1  # one company's one pinned filing, never a blend
        seen_filing_ids.append(filing_ids.pop())
    assert seen_filing_ids == [aapl_filing, msft_filing]  # caller order


def test_assembly_is_model_free_with_no_reentry(db: Engine) -> None:
    """Exactly one answer-model call per column-capable company — never a
    sixth 'summarize the columns' call — and no call's input contains model
    output: every input is retrieved evidence (ChunkMatch), so an answer-model
    output re-entering an answer-model call has no path (ADR-0012 #6a)."""
    seed_ready_company(db)
    seed_ready_company(
        db,
        ticker="MSFT",
        cik="0000789019",
        name="Microsoft Corporation",
        accession="0000789019-25-000001",
    )
    seed_ready_company(
        db,
        ticker="GOOG",
        cik="0001652044",
        name="Alphabet Inc.",
        accession="0001652044-25-000001",
    )
    spy = SpyAnswerer()

    result = compare_companies(db, query_embedder(), spy, ["AAPL", "MSFT", "GOOG"], "tariffs")

    column_entries = [entry for entry in result.entries if entry.kind == "column"]
    assert len(column_entries) == 3
    assert len(spy.calls) == 3  # one per column; assembly added zero calls
    for _query, chunks in spy.calls:
        assert isinstance(chunks, dict)
        for value in chunks.values():
            assert isinstance(value, ChunkMatch)  # evidence in, never a ColumnDraft


# --- named set, CIK dedup, >5 refusal (ADR-0012 #1) ---


def test_over_cap_refusal_runs_before_any_retrieval_or_synthesis(db: Engine) -> None:
    for i, ticker in enumerate(["AAPL", "MSFT", "GOOG", "AMZN", "NVDA", "META"]):
        seed_reference(db, ticker, f"000000000{i}", f"Company {ticker}")

    with pytest.raises(CompareRefusal, match="at most 5"):
        compare_companies(
            db,
            ExplodingEmbedder(),
            ExplodingAnswerer(),
            ["AAPL", "MSFT", "GOOG", "AMZN", "NVDA", "META"],
            "tariffs",
        )


def test_cap_counts_unresolved_names_too(db: Engine) -> None:
    """The bound is on the request, not on what triage leaves: 7 names are
    refused even when only 2 would resolve (ADR-0012 #1)."""
    seed_reference(db, "AAPL", "0000320193", "Apple Inc.")
    seed_reference(db, "MSFT", "0000789019", "Microsoft Corporation")

    with pytest.raises(CompareRefusal, match="at most 5"):
        compare_companies(
            db,
            ExplodingEmbedder(),
            ExplodingAnswerer(),
            ["AAPL", "MSFT", "NOPE1", "NOPE2", "NOPE3", "NOPE4", "NOPE5"],
            "tariffs",
        )


def test_shared_cik_tickers_fold_to_one_entry_labeled_first_named(db: Engine) -> None:
    seed_ready_company(db, ticker="GOOG", cik="0001652044", name="Alphabet Inc.")
    seed_reference(db, "GOOGL", "0001652044", "Alphabet Inc.")

    result = compare_companies(db, query_embedder(), SpyAnswerer(), ["GOOG", "GOOGL"], "tariffs")

    assert len(result.entries) == 1  # one company, one entry — never duplicate columns
    assert result.entries[0].symbol == "GOOG"  # first-named symbol labels it
    assert result.entries[0].kind == "column"


def test_shared_cik_dedup_happens_before_the_cap_check(db: Engine) -> None:
    """Six names where two share a CIK are five companies: not refused."""
    seed_reference(db, "GOOG", "0001652044", "Alphabet Inc.")
    seed_reference(db, "GOOGL", "0001652044", "Alphabet Inc.")
    for i, ticker in enumerate(["AAPL", "MSFT", "AMZN", "NVDA"]):
        seed_reference(db, ticker, f"000000000{i}", f"Company {ticker}")

    result = compare_companies(
        db,
        query_embedder(),
        SpyAnswerer(),
        ["GOOG", "GOOGL", "AAPL", "MSFT", "AMZN", "NVDA"],
        "tariffs",
    )

    assert len(result.entries) == 5


# --- one ordered entries list (ADR-0012 #2): prominence is structural ---


def test_one_typed_entry_per_deduplicated_name_in_caller_order(db: Engine) -> None:
    seed_ready_company(db)  # AAPL: column
    # NVDA resolves but holds no ingested 10-K: no_evidence
    seed_reference(db, "NVDA", "0001045810", "NVIDIA Corp")
    # "MISSING" is not in the reference at all: unresolved

    result = compare_companies(
        db,
        query_embedder(),
        SpyAnswerer(),
        ["AAPL", "MISSING", "NVDA", "aapl"],  # duplicate symbol dedupes, case-insensitive
        "tariffs",
    )

    assert [entry.kind for entry in result.entries] == ["column", "unresolved", "no_evidence"]
    assert [entry.symbol for entry in result.entries] == ["AAPL", "MISSING", "NVDA"]
    assert len(result.entries) == 3  # len(entries) == len(deduplicated names), always
    assert result.partial is True


def test_partial_is_false_when_every_entry_is_evidence(db: Engine) -> None:
    seed_ready_company(db)
    result = compare_companies(db, query_embedder(), SpyAnswerer(), ["AAPL"], "tariffs")
    assert result.partial is False
    assert result.entries[0].company_name == "Apple Inc."
    assert result.entries[0].cik == "0000320193"


# --- latest-10-K pinning (ADR-0012 #3) ---


def test_column_pins_to_latest_10k_not_a_ticker_scoped_blend(db: Engine) -> None:
    """AAPL holds FY2024 and FY2025 10-Ks, and the FY2024 chunks match the
    query BETTER. A ticker-scoped search would feed the column FY2024 text;
    filing-pinned retrieval must cite FY2025 only (ADR-0012 #3)."""
    seed_reference(db, "AAPL", "0000320193", "Apple Inc.")
    fy2024 = seed_company_filing(
        db,
        ticker="AAPL",
        cik="0000320193",
        name="Apple Inc.",
        accession="0000320193-24-000001",
        filing_date="2024-11-01",
        period_end_date="2024-09-28",
        filing_url="https://sec.gov/aapl-fy2024.htm",
    )
    fy2025 = seed_company_filing(
        db,
        ticker="AAPL",
        cik="0000320193",
        name="Apple Inc.",
        accession="0000320193-25-000001",
        filing_date="2025-10-31",
        period_end_date="2025-09-27",
        filing_url="https://sec.gov/aapl-fy2025.htm",
    )
    seed_chunks(
        db,
        fy2024,
        [("fy2024 strong tariff text", blend_vector(0.95))],
        source_url="https://sec.gov/aapl-fy2024.htm",
    )
    seed_chunks(
        db,
        fy2025,
        [("fy2025 tariff text", blend_vector(0.5))],
        source_url="https://sec.gov/aapl-fy2025.htm",
    )

    result = compare_companies(db, query_embedder(), SpyAnswerer(), ["AAPL"], "tariffs")

    (entry,) = result.entries
    assert entry.kind == "column"
    assert entry.filing is not None
    assert entry.filing.period_end_date == date(2025, 9, 27)
    assert entry.statements  # the column did synthesize
    for statement in entry.statements:
        assert statement.source_url == "https://sec.gov/aapl-fy2025.htm"


def test_10ka_amendment_speaks_for_the_period(db: Engine) -> None:
    seed_reference(db, "AAPL", "0000320193", "Apple Inc.")
    original = seed_company_filing(
        db,
        ticker="AAPL",
        cik="0000320193",
        name="Apple Inc.",
        accession="0000320193-25-000001",
        form_type="10-K",
        filing_date="2025-10-31",
        period_end_date="2025-09-27",
        filing_url="https://sec.gov/aapl-original.htm",
    )
    amendment = seed_company_filing(
        db,
        ticker="AAPL",
        cik="0000320193",
        name="Apple Inc.",
        accession="0000320193-25-000099",
        form_type="10-K/A",
        filing_date="2025-12-15",
        period_end_date="2025-09-27",
        filing_url="https://sec.gov/aapl-amended.htm",
    )
    seed_chunks(
        db,
        original,
        [("original text", blend_vector(0.9))],
        source_url="https://sec.gov/aapl-original.htm",
    )
    seed_chunks(
        db,
        amendment,
        [("amended text", blend_vector(0.9))],
        source_url="https://sec.gov/aapl-amended.htm",
    )

    result = compare_companies(db, query_embedder(), SpyAnswerer(), ["AAPL"], "tariffs")

    (entry,) = result.entries
    assert entry.filing is not None
    assert entry.filing.form_type == "10-K/A"  # the corrected statement wins
    for statement in entry.statements:
        assert statement.source_url == "https://sec.gov/aapl-amended.htm"


def test_no_evidence_means_no_ingested_10k_not_no_filings(db: Engine) -> None:
    """A company holding only a 10-Q resolves and has evidence — but no 10-K
    to pin a column to, which is no_evidence for COMPARE (ADR-0012 #2)."""
    seed_reference(db, "AAPL", "0000320193", "Apple Inc.")
    ten_q = seed_company_filing(
        db,
        ticker="AAPL",
        cik="0000320193",
        name="Apple Inc.",
        accession="0000320193-25-000042",
        form_type="10-Q",
    )
    seed_chunks(db, ten_q, [("quarterly text", blend_vector(0.9))])
    spy = SpyAnswerer()

    result = compare_companies(db, query_embedder(), spy, ["AAPL"], "tariffs")

    (entry,) = result.entries
    assert entry.kind == "no_evidence"
    assert spy.calls == []  # no filing to pin → no retrieval consult, no model


# --- coverage signal and the empty-column split (ADR-0012 #3, #5) ---


def test_coverage_reports_qualifying_vs_consulted(db: Engine) -> None:
    seed_reference(db, "AAPL", "0000320193", "Apple Inc.")
    filing_id = seed_company_filing(
        db, ticker="AAPL", cik="0000320193", name="Apple Inc.", accession="0000320193-25-000001"
    )
    seed_chunks(
        db,
        filing_id,
        [
            ("on topic 1", blend_vector(0.9)),
            ("on topic 2", blend_vector(0.6)),
            ("on topic 3", blend_vector(0.3)),
            ("off topic 1", axis_vector(1)),  # similarity 0.0: below the floor
            ("off topic 2", axis_vector(2)),
        ],
    )

    result = compare_companies(db, query_embedder(), SpyAnswerer(), ["AAPL"], "tariffs")

    (entry,) = result.entries
    assert entry.kind == "column"
    assert entry.coverage is not None
    assert entry.coverage.qualifying == 3
    assert entry.coverage.consulted == 2  # PASSAGES_PER_COMPANY_COMPARE
    # Floor-legible display (build review finding #1): the scanned count and
    # the floor in force ride with the counts, so "2 of 2" can never silently
    # mean "2 of 2 above an uncalibrated threshold".
    assert entry.coverage.scanned == 5
    assert entry.coverage.floor == QUALIFYING_SIMILARITY_FLOOR
    assert len(entry.statements) == 2


def test_below_floor_no_finding_never_calls_the_model(db: Engine) -> None:
    """Chunks exist but none clear the floor: a low-confidence retrieval
    state, distinct from both silence-after-consulting (model_declined) and
    corpus state (no_embedded_evidence) — the three-way split of build
    review finding #3. Mechanical: no model call."""
    seed_reference(db, "AAPL", "0000320193", "Apple Inc.")
    filing_id = seed_company_filing(
        db, ticker="AAPL", cik="0000320193", name="Apple Inc.", accession="0000320193-25-000001"
    )
    seed_chunks(db, filing_id, [("unrelated", axis_vector(1)), ("also unrelated", axis_vector(2))])
    spy = SpyAnswerer()

    result = compare_companies(db, query_embedder(), spy, ["AAPL"], "tariffs")

    (entry,) = result.entries
    assert entry.kind == "no_finding"
    assert entry.no_finding_cause == "below_floor"
    assert entry.coverage is not None
    assert (entry.coverage.qualifying, entry.coverage.consulted) == (0, 0)
    assert entry.coverage.scanned == 2  # chunks existed; none qualified
    assert spy.calls == []  # a mechanical fact needs no model


def test_unembedded_filing_is_corpus_state_not_filing_silence(db: Engine) -> None:
    """A pinned filing with NO embedded chunks (backfill pending, extraction
    gap) must not masquerade as 'the filing does not address this' — that is
    corpus state, not filing content (honest-absence, build review finding
    #3)."""
    seed_reference(db, "AAPL", "0000320193", "Apple Inc.")
    seed_company_filing(
        db, ticker="AAPL", cik="0000320193", name="Apple Inc.", accession="0000320193-25-000001"
    )
    # deliberately no seed_chunks: the filing is held but not embedded
    spy = SpyAnswerer()

    result = compare_companies(db, query_embedder(), spy, ["AAPL"], "tariffs")

    (entry,) = result.entries
    assert entry.kind == "no_finding"
    assert entry.no_finding_cause == "no_embedded_evidence"
    assert entry.coverage is not None
    assert entry.coverage.scanned == 0  # nothing to scan — the tell
    assert spy.calls == []


def test_model_declined_attaches_consulted_passages_for_audit(db: Engine) -> None:
    seed_ready_company(db)
    declining = SpyAnswerer(draft=ColumnDraft(claims=[]))

    result = compare_companies(db, query_embedder(), declining, ["AAPL"], "tariffs")

    (entry,) = result.entries
    assert entry.kind == "no_finding"
    assert entry.no_finding_cause == "model_declined"
    assert entry.coverage is not None
    assert entry.coverage.consulted == len(entry.consulted_passages) == 2
    for passage in entry.consulted_passages:
        assert passage.snippet
        assert passage.source_url.startswith("https://sec.gov/")


# --- citation binding: the draft/resolved split (ADR-0012 #4) ---


def test_unciteable_claims_are_dropped_with_warnings_never_emitted(db: Engine) -> None:
    seed_ready_company(db)
    answerer = SpyAnswerer(
        draft=ColumnDraft(
            claims=[
                ColumnClaim(text="cited fine", chunk_ids=["C1"]),
                ColumnClaim(text="cites a chunk that was never supplied", chunk_ids=["C9"]),
                ColumnClaim(text="cites nothing at all"),
            ]
        )
    )

    result = compare_companies(db, query_embedder(), answerer, ["AAPL"], "tariffs")

    (entry,) = result.entries
    assert entry.kind == "column"
    assert [statement.text for statement in entry.statements] == ["cited fine"]
    assert entry.statements[0].source_url == "https://sec.gov/aapl-fy2025.htm"  # stamped from DB
    assert len(entry.warnings) == 2  # both drops reported, never silently swallowed


def test_all_claims_unciteable_is_claims_uncited_not_a_decline(db: Engine) -> None:
    """A model that asserted claims it could not cite did NOT decline — it
    produced unauditable content that was suppressed. Labeling that
    model_declined would dress a suppressed-hallucination case as an honest
    'nothing to say' (build review finding #4). Distinct cause, consulted
    passages and warnings attached, so the suppression is auditable."""
    seed_ready_company(db)
    answerer = SpyAnswerer(draft=ColumnDraft(claims=[ColumnClaim(text="uncited prose")]))

    result = compare_companies(db, query_embedder(), answerer, ["AAPL"], "tariffs")

    (entry,) = result.entries
    assert entry.kind == "no_finding"
    assert entry.no_finding_cause == "claims_uncited"
    assert entry.consulted_passages  # the suppression stays auditable
    assert entry.warnings  # every drop named


# --- caps and constants (ADR-0012 #3, ADR-0011 §3 posture) ---


def test_passages_cap_is_a_ceiling_not_a_request_knob(db: Engine) -> None:
    seed_reference(db, "AAPL", "0000320193", "Apple Inc.")
    filing_id = seed_company_filing(
        db, ticker="AAPL", cik="0000320193", name="Apple Inc.", accession="0000320193-25-000001"
    )
    seed_chunks(db, filing_id, [(f"chunk {i}", blend_vector(0.9 - i * 0.1)) for i in range(5)])

    result = compare_companies(
        db, query_embedder(), SpyAnswerer(), ["AAPL"], "tariffs", passages_per_company=999
    )

    (entry,) = result.entries
    assert entry.coverage is not None
    assert entry.coverage.consulted == PASSAGES_PER_COMPANY_COMPARE  # 999 clamped to the ceiling


def test_compare_rejects_bad_inputs(db: Engine) -> None:
    with pytest.raises(ValueError, match="query"):
        compare_companies(db, ExplodingEmbedder(), ExplodingAnswerer(), ["AAPL"], "   ")
    with pytest.raises(ValueError, match="ticker"):
        compare_companies(db, ExplodingEmbedder(), ExplodingAnswerer(), [], "tariffs")
    with pytest.raises(ValueError, match="positive"):
        compare_companies(
            db,
            ExplodingEmbedder(),
            ExplodingAnswerer(),
            ["AAPL"],
            "tariffs",
            passages_per_company=0,
        )


def test_compare_constants_match_adr_0012() -> None:
    assert MAX_COMPARE_COMPANIES == 5
    assert PASSAGES_PER_COMPANY_COMPARE == 2
    assert 0.0 < QUALIFYING_SIMILARITY_FLOOR < 1.0


# --- the mock (ADR-0012 mocked build): deterministic, single swap point ---


def test_mock_answerer_is_deterministic_and_cites_only_supplied_labels(db: Engine) -> None:
    chunk = ChunkMatch(
        ticker="AAPL",
        filing_id=1,
        section="risk_factors",
        chunk_index=0,
        content="tariff exposure text",
        source_url="https://sec.gov/x.htm",
        distance=0.1,
    )
    mock = MockColumnAnswerer()

    first = mock.draft_column("tariffs", {"C1": chunk})
    second = mock.draft_column("tariffs", {"C1": chunk})

    assert first == second  # deterministic: reviewable without live spend
    assert [claim.chunk_ids for claim in first.claims] == [["C1"]]
