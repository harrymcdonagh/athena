"""Cited question-answering over filing chunks.

Policy is ADR-0007 (docs/decisions/0007-cited-question-answering-no-verdicts.md):
analytical but grounded, chunks-only evidence, two-sided case for buy/sell-shaped
questions, never a verdict. The prompt text below is load-bearing policy per that
ADR's final consequence — review changes against the ADR, not as copy tweaks.
"""

import argparse
import re
from dataclasses import dataclass
from typing import Literal, Protocol

import anthropic
from pydantic import BaseModel, Field
from sqlalchemy import Engine, create_engine

from apps.api.config import get_settings
from apps.api.research.embeddings import Embedder, VoyageEmbedder, semantic_search
from apps.api.research.repository import ChunkMatch

_MODEL = "claude-opus-4-8"

# The system policy block. Each directive traces to ADR-0007 (§ references inline).
_SYSTEM_PROMPT = (
    "You are Athena's question-answering layer for SEC filing research. You answer"
    " questions about companies using the evidence chunks supplied in the user"
    " message, each labelled C1, C2, ...\n"
    "\n"
    "Grounding — hard constraint (ADR-0007 §4):\n"
    "- Use ONLY the supplied chunks. Your background knowledge about any company,"
    " its financials, its stock, or its industry is off-limits, even when you are"
    " certain it is correct.\n"
    "- Every claim must cite the chunk label(s) it rests on via chunk_ids.\n"
    '- If the supplied chunks do not address the question, use mode "insufficient_evidence"'
    " and describe what the retrieved filings do and do not cover. Never fall back"
    " to memory.\n"
    "\n"
    "Temporal comparisons (ADR-0007 §5):\n"
    '- A "what changed since last quarter/year" claim is grounded only if the supplied'
    " chunks span multiple filing periods for the same company. If they do not, use"
    ' mode "no_prior_period": the retrieved filings do not include a prior period to'
    " compare. Never synthesize a comparison from memory, even if you know the"
    " prior-period figures.\n"
    "\n"
    "You MAY (ADR-0007 §1):\n"
    "- group evidence by theme;\n"
    "- note agreement or contradiction across filings;\n"
    "- connect a stated figure to a cause the filing itself states;\n"
    "- frame what different investor lenses (growth, value, credit) would weigh;\n"
    "- report a filing's own evaluative language, explicitly attributed and cited"
    ' (e.g. "management characterizes liquidity as strong"). Do not scrub the'
    " documents' voice; attribute it.\n"
    "\n"
    "You MAY NOT (ADR-0007 §3):\n"
    "- give a buy/sell/hold recommendation, price target, or investment advice of"
    " any kind;\n"
    "- rank companies by attractiveness;\n"
    '- adopt an evaluative stance in your own voice ("strong", "concerning",'
    ' "impressive"). Reporting the source\'s framing, attributed, is fine;'
    " editorializing is banned.\n"
    "\n"
    'Buy/sell-shaped questions ("should I buy X?", "is X a good investment?")'
    " (ADR-0007 §2):\n"
    '- Do not refuse and do not give a verdict. Use mode "two_sided": a cited bull'
    " case, a cited bear case, what changed since the prior filing (empty when the"
    " chunks hold no prior period), and state in verdict_note that the verdict is"
    " the user's to make.\n"
    "\n"
    'Modes: "direct" for ordinary factual or analytical questions; "two_sided" for'
    ' buy/sell-shaped questions; "insufficient_evidence" when the chunks do not'
    ' address the question; "no_prior_period" for temporal comparisons without a'
    " prior period in the chunks."
)


class QaError(Exception):
    pass


class Claim(BaseModel):
    """One unit of the answer, tagged with the chunk label(s) it rests on."""

    text: str
    chunk_ids: list[str] = Field(default_factory=list)


class QaAnswer(BaseModel):
    """Structured answer shape. Deliberately has no verdict/recommendation field:
    a buy/sell verdict is structurally unrepresentable (ADR-0007 §3)."""

    mode: Literal["direct", "two_sided", "insufficient_evidence", "no_prior_period"]
    claims: list[Claim] = Field(default_factory=list)  # direct mode
    bull: list[Claim] = Field(default_factory=list)  # two_sided mode
    bear: list[Claim] = Field(default_factory=list)
    what_changed: list[Claim] = Field(default_factory=list)
    verdict_note: str = ""  # two_sided: explicit verdict-is-the-user's statement
    explanation: str = Field(
        default="",
        description=(
            "Brief plain-text note (1-3 sentences) on what the retrieved chunks do"
            " and do not cover — retrieval scope and limits only. Used mainly for"
            " insufficient_evidence and no_prior_period modes. Never include your"
            " reasoning process, drafting notes, or self-corrections, and never"
            " HTML or markup."
        ),
    )


@dataclass(frozen=True)
class QaWarning:
    kind: Literal["uncited_claim", "unknown_citation", "reasoning_artifact"]
    message: str


# Leak classes for the free-text guard in verify_answer. Athena's own meta-prose
# (explanation, verdict_note) should never contain markup, first-person process
# talk, or self-correction fragments — their presence means model reasoning
# leaked into user-facing output. Extend the list as new leak shapes appear;
# detection flags a warning, it never rewrites the text.
_ARTIFACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # HTML/markup (e.g. a stray <br>)
    re.compile(r"</?[a-zA-Z][^>\n]*>"),
    # first-person process talk ("I already produced...", "let me re-check")
    re.compile(
        r"(?i)\b(?:let me|i'(?:ll|ve|m)"
        r"|i\s+(?:already|will|need|should|apologize|made|produced|answered|am))\b"
    ),
    # self-correction / meta-instruction fragments addressed to no reader
    re.compile(
        r"(?i)\b(?:no change needed|re-?examine|relook|on second thought|disregard"
        r"|as an ai|system (?:message|prompt)|my previous (?:answer|response))\b"
    ),
)


@dataclass(frozen=True)
class QaResult:
    answer: QaAnswer
    citations: dict[str, ChunkMatch]  # chunk label -> retrieved chunk (with source_url)
    warnings: list[QaWarning]  # first-class; never swallowed


class QaAnswerer(Protocol):
    def answer(self, question: str, chunks: dict[str, ChunkMatch]) -> QaAnswer: ...


def build_qa_prompt(question: str, chunks: dict[str, ChunkMatch]) -> str:
    rendered = "\n\n".join(
        f'<chunk label="{label}" ticker="{match.ticker}" section="{match.section}"'
        f' source_url="{match.source_url}">\n{match.content}\n</chunk>'
        for label, match in chunks.items()
    )
    return f"Question: {question}\n\nEvidence chunks (the ONLY material you may use):\n\n{rendered}"


class ClaudeQaAnswerer:
    model = _MODEL

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)

    def answer(self, question: str, chunks: dict[str, ChunkMatch]) -> QaAnswer:
        response = self._client.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_qa_prompt(question, chunks)}],
            output_format=QaAnswer,
        )
        answer = response.parsed_output
        if answer is None:
            raise QaError(f"model returned no parsed answer (stop_reason={response.stop_reason!r})")
        return answer


def _all_claims(answer: QaAnswer) -> list[Claim]:
    return [*answer.claims, *answer.bull, *answer.bear, *answer.what_changed]


def verify_answer(answer: QaAnswer, retrieved: dict[str, ChunkMatch]) -> list[QaWarning]:
    """Lightweight verification (ADR-0007 lightweight enforcement).

    Checks that every claim is CITED and that every citation points at a chunk
    that was actually retrieved, and that the free-text fields (explanation,
    verdict_note) carry no leaked-reasoning artifacts. It does NOT check that
    the cited chunk supports the claim — that semantic check is future work per
    ADR-0007's Consequences. Findings are flagged as warnings; the answer text
    is never altered.
    """
    warnings: list[QaWarning] = []
    for field_name, value in (
        ("explanation", answer.explanation),
        ("verdict_note", answer.verdict_note),
    ):
        for pattern in _ARTIFACT_PATTERNS:
            found = pattern.search(value)
            if found:
                warnings.append(
                    QaWarning(
                        kind="reasoning_artifact",
                        message=(
                            f"{field_name} contains a suspected leaked-reasoning"
                            f" artifact: {found.group(0)!r}"
                        ),
                    )
                )
    for claim in _all_claims(answer):
        if not claim.chunk_ids:
            warnings.append(
                QaWarning(
                    kind="uncited_claim",
                    message=f"claim has no citation: {claim.text!r}",
                )
            )
        for chunk_id in claim.chunk_ids:
            if chunk_id not in retrieved:
                warnings.append(
                    QaWarning(
                        kind="unknown_citation",
                        message=(
                            f"claim cites {chunk_id}, which is not in the retrieved"
                            f" set: {claim.text!r}"
                        ),
                    )
                )
    return warnings


def answer_question(
    engine: Engine,
    embedder: Embedder,
    answerer: QaAnswerer,
    question: str,
    limit: int = 8,
    *,
    ticker: str | None = None,
    section: str | None = None,
) -> QaResult:
    if not question.strip():
        raise ValueError("question must not be blank")
    matches = semantic_search(engine, embedder, question, limit, ticker=ticker, section=section)
    if not matches:
        return QaResult(
            answer=QaAnswer(
                mode="insufficient_evidence",
                explanation="No relevant chunks were retrieved for this question.",
            ),
            citations={},
            warnings=[],
        )
    chunks = {f"C{i}": match for i, match in enumerate(matches, start=1)}
    try:
        answer = answerer.answer(question, chunks)
    except anthropic.APIError as exc:
        raise QaError(f"anthropic: {exc}") from exc
    return QaResult(answer=answer, citations=chunks, warnings=verify_answer(answer, chunks))


def _print_claims(title: str, claims: list[Claim]) -> None:
    if not claims:
        return
    print(f"\n{title}:")
    for claim in claims:
        cited = ", ".join(claim.chunk_ids) if claim.chunk_ids else "UNCITED"
        print(f"  - {claim.text}  [{cited}]")


def _print_result(result: QaResult) -> None:
    print(f"mode: {result.answer.mode}")
    _print_claims("answer", result.answer.claims)
    _print_claims("bull case", result.answer.bull)
    _print_claims("bear case", result.answer.bear)
    _print_claims("what changed", result.answer.what_changed)
    if result.answer.verdict_note:
        print(f"\nverdict note: {result.answer.verdict_note}")
    if result.answer.explanation:
        print(f"\nexplanation: {result.answer.explanation}")
    if result.citations:
        print("\ncitations:")
        for label, match in result.citations.items():
            print(
                f"  {label}: {match.ticker} {match.section} chunk {match.chunk_index}"
                f" (distance {match.distance:.3f})"
            )
            print(f"      {match.source_url}")
    if result.warnings:
        print("\nwarnings:")
        for warning in result.warnings:
            print(f"  [{warning.kind}] {warning.message}")
    else:
        print("\nwarnings: none")


def main() -> None:
    """Manual acceptance runner (live Voyage + Claude calls) — see ADR-0007."""
    parser = argparse.ArgumentParser(
        description="Ask a cited question over the ingested filing corpus."
    )
    parser.add_argument("question")
    parser.add_argument("--ticker", default=None, help="restrict retrieval to one ticker")
    parser.add_argument("--limit", type=int, default=8, help="chunks to retrieve (default 8)")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.voyage_api_key:
        raise SystemExit("VOYAGE_API_KEY is not set; it is needed to embed the query.")
    if not settings.anthropic_api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set; it is needed for the Claude call.")

    engine = create_engine(settings.database_url)
    embedder = VoyageEmbedder(api_key=settings.voyage_api_key)
    answerer = ClaudeQaAnswerer(api_key=settings.anthropic_api_key)
    result = answer_question(
        engine, embedder, answerer, args.question, args.limit, ticker=args.ticker
    )
    _print_result(result)


if __name__ == "__main__":
    main()
