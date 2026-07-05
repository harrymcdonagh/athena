"""Tests for the cited question-answering layer governed by ADR-0007.

Written before the implementation (TDD). The Claude call is mocked everywhere —
no live LLM calls in this suite. These tests verify the pipeline and the
lightweight verification pass (citations exist and point at retrieved chunks);
they cannot verify that the live model obeys the ADR-0007 policy — that
evaluation is future enforcement work per the ADR's Consequences.
"""

from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import anthropic

# anthropic SDK exception types are bound to real httpx; do not use httpx2 here.
import httpx
import pytest
from sqlalchemy import Engine

from apps.api.research import qa
from apps.api.research.embeddings import Embedder
from apps.api.research.qa import (
    Claim,
    ClaudeQaAnswerer,
    QaAnswer,
    QaError,
    answer_question,
    build_qa_prompt,
    verify_answer,
)
from apps.api.research.repository import ChunkMatch
from apps.api.tests.test_embeddings import (
    QueryOnlyEmbedder,
    axis_vector,
    seed_axis_chunks,
    seed_filing,
)

# answer_question only touches these through semantic_search; tests that stub
# semantic_search (or expect it to raise before any I/O) never dereference them.
ENGINE = cast(Engine, object())
EMBEDDER = cast(Embedder, object())


def chunk(index: int) -> ChunkMatch:
    return ChunkMatch(
        ticker="AAPL",
        filing_id=1,
        section="mdna",
        chunk_index=index,
        content=f"Net sales fact {index} as stated in the filing.",
        source_url="https://sec.gov/filing.htm",
        distance=0.1,
    )


def make_chunks(n: int) -> dict[str, ChunkMatch]:
    return {f"C{i}": chunk(i) for i in range(1, n + 1)}


def stub_search(matches: list[ChunkMatch]) -> Callable[..., list[ChunkMatch]]:
    def _search(
        engine: Engine,
        embedder: Embedder,
        query: str,
        limit: int = 8,
        *,
        ticker: str | None = None,
        section: str | None = None,
    ) -> list[ChunkMatch]:
        return matches

    return _search


class FakeQaAnswerer:
    def __init__(self, answer: QaAnswer) -> None:
        self._answer = answer
        self.calls: list[tuple[str, dict[str, ChunkMatch]]] = []

    def answer(self, question: str, chunks: dict[str, ChunkMatch]) -> QaAnswer:
        self.calls.append((question, chunks))
        return self._answer


class AnthropicErrorAnswerer:
    def answer(self, question: str, chunks: dict[str, ChunkMatch]) -> QaAnswer:
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.APIConnectionError(request=request)


TWO_SIDED = QaAnswer(
    mode="two_sided",
    bull=[Claim(text="Services revenue grew 13%.", chunk_ids=["C1"])],
    bear=[Claim(text="The filing names supply chain concentration as a risk.", chunk_ids=["C2"])],
    what_changed=[],
    verdict_note=(
        "This is the cited case on both sides, not a recommendation; the verdict is yours to make."
    ),
)


# --- verify_answer (pure, no mocks) ---


def test_verify_answer_passes_fully_cited_answer() -> None:
    retrieved = make_chunks(3)
    answer = QaAnswer(
        mode="direct",
        claims=[Claim(text="Revenue grew, driven by services.", chunk_ids=["C1", "C3"])],
    )
    assert verify_answer(answer, retrieved) == []


def test_verify_answer_flags_uncited_claim() -> None:
    retrieved = make_chunks(1)
    answer = QaAnswer(mode="direct", claims=[Claim(text="Margins expanded.", chunk_ids=[])])
    warnings = verify_answer(answer, retrieved)
    assert [w.kind for w in warnings] == ["uncited_claim"]
    assert "Margins expanded." in warnings[0].message


def test_verify_answer_flags_citation_outside_retrieved_set() -> None:
    retrieved = make_chunks(3)
    answer = QaAnswer(mode="direct", claims=[Claim(text="Something.", chunk_ids=["C99"])])
    warnings = verify_answer(answer, retrieved)
    assert [w.kind for w in warnings] == ["unknown_citation"]
    assert "C99" in warnings[0].message


def test_verify_answer_covers_every_two_sided_claim_list() -> None:
    retrieved = make_chunks(2)
    answer = QaAnswer(
        mode="two_sided",
        bull=[Claim(text="Cited bull point.", chunk_ids=["C1"])],
        bear=[Claim(text="Uncited bear point.", chunk_ids=[])],
        what_changed=[Claim(text="Fabricated comparison.", chunk_ids=["C9"])],
        verdict_note="The verdict is yours.",
    )
    kinds = sorted(w.kind for w in verify_answer(answer, retrieved))
    assert kinds == ["uncited_claim", "unknown_citation"]


# --- adversarial pipeline tests (ADR-0007) ---


def test_buy_sell_question_gets_two_sided_case_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qa, "semantic_search", stub_search([chunk(i) for i in range(1, 4)]))
    answerer = FakeQaAnswerer(TWO_SIDED)

    result = answer_question(ENGINE, EMBEDDER, answerer, "Should I buy NVDA?")

    assert result.answer.mode == "two_sided"
    assert result.answer.bull and result.answer.bear
    assert "verdict is yours" in result.answer.verdict_note
    # A verdict is structurally unrepresentable: the schema has no field for one.
    for forbidden in ("recommendation", "rating", "action", "price_target", "verdict"):
        assert forbidden not in QaAnswer.model_fields
    assert result.warnings == []


def test_what_changed_without_prior_period_is_not_fabricated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matches = [chunk(i) for i in range(1, 4)]  # single-filing corpus: one period only
    monkeypatch.setattr(qa, "semantic_search", stub_search(matches))
    answerer = FakeQaAnswerer(
        QaAnswer(
            mode="no_prior_period",
            explanation=(
                "The retrieved filings cover only fiscal 2025; there is no prior period to compare."
            ),
        )
    )

    result = answer_question(ENGINE, EMBEDDER, answerer, "What changed since last year at AAPL?")

    assert result.answer.mode == "no_prior_period"
    assert "no prior period" in result.answer.explanation
    # The answerer saw exactly the retrieved chunks — no injected background context.
    (call,) = answerer.calls
    question, chunks = call
    assert question == "What changed since last year at AAPL?"
    assert list(chunks) == ["C1", "C2", "C3"]
    assert [c.content for c in chunks.values()] == [m.content for m in matches]


def test_fabricated_citation_yields_visible_warning_not_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qa, "semantic_search", stub_search([chunk(i) for i in range(1, 4)]))
    answerer = FakeQaAnswerer(
        QaAnswer(
            mode="direct",
            claims=[
                Claim(text="Claim resting on a fabricated citation.", chunk_ids=["C99"]),
                Claim(text="Claim resting on real evidence.", chunk_ids=["C1"]),
            ],
        )
    )

    result = answer_question(ENGINE, EMBEDDER, answerer, "What drives revenue?")

    assert [w.kind for w in result.warnings] == ["unknown_citation"]
    assert "C99" in result.warnings[0].message
    # The answer is surfaced intact alongside the warning — flagged, not swallowed.
    assert [c.text for c in result.answer.claims] == [
        "Claim resting on a fabricated citation.",
        "Claim resting on real evidence.",
    ]


def test_empty_retrieval_short_circuits_to_insufficient_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qa, "semantic_search", stub_search([]))
    answerer = FakeQaAnswerer(TWO_SIDED)

    result = answer_question(ENGINE, EMBEDDER, answerer, "What does the filing say about tofu?")

    assert result.answer.mode == "insufficient_evidence"
    assert result.citations == {}
    assert result.warnings == []
    assert answerer.calls == []  # no Claude call on empty evidence


def test_anthropic_api_error_becomes_qa_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa, "semantic_search", stub_search([chunk(1)]))
    with pytest.raises(QaError):
        answer_question(ENGINE, EMBEDDER, AnthropicErrorAnswerer(), "What drives revenue?")


def test_blank_question_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub retrieval so this exercises answer_question's own guard, not semantic_search's.
    monkeypatch.setattr(qa, "semantic_search", stub_search([chunk(1)]))
    with pytest.raises(ValueError):
        answer_question(ENGINE, EMBEDDER, FakeQaAnswerer(TWO_SIDED), "   ")


# --- retrieval reuse + citation resolution (real test database) ---


def test_answer_question_reuses_semantic_search_and_resolves_citations(db: Engine) -> None:
    filing_id = seed_filing(db)
    seed_axis_chunks(db, filing_id)
    embedder = QueryOnlyEmbedder(axis_vector(1))
    answerer = FakeQaAnswerer(
        QaAnswer(mode="direct", claims=[Claim(text="Topic one fact.", chunk_ids=["C1"])])
    )

    result = answer_question(db, embedder, answerer, "what about topic 1?", limit=3)

    # Labels are assigned in retrieval order; the nearest chunk is C1.
    assert list(result.citations) == ["C1", "C2", "C3"]
    assert result.citations["C1"].content == "topic 1"
    # Every citation resolves to a source_url: the audit chain survives the QA layer.
    assert all(match.source_url for match in result.citations.values())
    assert result.warnings == []


# --- ClaudeQaAnswerer (monkeypatched anthropic client, no network) ---


class FakeMessages:
    def __init__(self, answer: QaAnswer | None) -> None:
        self._answer = answer
        self.parse_kwargs: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> SimpleNamespace:
        self.parse_kwargs.append(kwargs)
        return SimpleNamespace(parsed_output=self._answer, stop_reason="end_turn")


class FakeAnthropicClient:
    def __init__(self, answer: QaAnswer | None) -> None:
        self.messages = FakeMessages(answer)


def make_claude_answerer(
    monkeypatch: pytest.MonkeyPatch, answer: QaAnswer | None
) -> tuple[ClaudeQaAnswerer, FakeAnthropicClient]:
    fake = FakeAnthropicClient(answer)
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: fake)
    return ClaudeQaAnswerer(api_key="test-key"), fake


def test_claude_answerer_sends_policy_and_only_retrieved_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answerer, fake = make_claude_answerer(monkeypatch, TWO_SIDED)
    chunks = make_chunks(2)

    result = answerer.answer("Should I buy AAPL?", chunks)

    assert result is TWO_SIDED
    (kwargs,) = fake.messages.parse_kwargs
    assert kwargs["model"] == ClaudeQaAnswerer.model
    assert kwargs["output_format"] is QaAnswer
    system = cast(str, kwargs["system"])
    # The ADR-0007 policy directives the system block must carry:
    assert "two_sided" in system  # buy/sell-shaped questions → two-sided cited case
    assert "ONLY" in system  # grounding: supplied chunks only, no background knowledge
    assert "no_prior_period" in system  # temporal gate
    assert "recommendation" in system  # MAY-NOT: no buy/sell/hold verdicts
    messages = cast(list[dict[str, str]], kwargs["messages"])
    (message,) = messages
    assert message["role"] == "user"
    # The user turn is exactly the question plus the retrieved chunks — nothing else.
    assert message["content"] == build_qa_prompt("Should I buy AAPL?", chunks)


def test_build_qa_prompt_renders_question_labels_and_provenance() -> None:
    chunks = make_chunks(2)
    prompt = build_qa_prompt("What drives revenue?", chunks)
    assert "What drives revenue?" in prompt
    for label, match in chunks.items():
        assert label in prompt
        assert match.content in prompt
        assert match.source_url in prompt
        assert match.section in prompt


def test_claude_answerer_raises_qa_error_when_nothing_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answerer, _ = make_claude_answerer(monkeypatch, None)
    with pytest.raises(QaError):
        answerer.answer("What drives revenue?", make_chunks(1))
