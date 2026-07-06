"""Cited question-answering over filing chunks.

Policy is ADR-0007 (docs/decisions/0007-cited-question-answering-no-verdicts.md):
analytical but grounded, chunks-only evidence, two-sided case for buy/sell-shaped
questions, never a verdict. The prompt text below is load-bearing policy per that
ADR's final consequence — review changes against the ADR, not as copy tweaks.
"""

import argparse
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol

import anthropic
from pydantic import BaseModel, Field
from sqlalchemy import Engine, create_engine

from apps.api.config import get_settings
from apps.api.research.embeddings import (
    Embedder,
    VoyageEmbedder,
    balanced_semantic_search,
    semantic_search,
)
from apps.api.research.repository import ChunkMatch, Repository

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
    kind: Literal[
        "uncited_claim", "unknown_citation", "reasoning_artifact", "missing_period_citation"
    ]
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


def _artifact_warnings(field_name: str, value: str) -> list[QaWarning]:
    """Flag (never strip) leaked-reasoning artifacts in a free-text field."""
    warnings: list[QaWarning] = []
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
    return warnings


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

    def compare(
        self, question: str, chunks: dict[str, ChunkMatch], periods: "Mapping[int, date]"
    ) -> "ComparisonDraft":
        response = self._client.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=_COMPARISON_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": build_comparison_prompt(question, chunks, periods)}
            ],
            output_format=ComparisonDraft,
        )
        draft = response.parsed_output
        if draft is None:
            raise QaError(f"model returned no parsed draft (stop_reason={response.stop_reason!r})")
        return draft


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
        warnings.extend(_artifact_warnings(field_name, value))
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


# --- change detection (ADR-0009): grounded comparison at QA time ---
#
# ADDITIVE to ADR-0007: the comparison shapes below are separate from QaAnswer,
# so the flag-off QA path keeps its exact schema (including what_changed as
# list[Claim]) and its model-facing output format, byte for byte.
#
# The model-facing draft carries chunk LABELS only — no URLs, no dates — so
# provenance is stamped mechanically from retrieval, never generated.

_COMPARISON_SYSTEM_PROMPT = (
    "You are Athena's change-detection layer for SEC filing research. You compare"
    " what a company's filings state across periods, using ONLY the evidence"
    " chunks supplied in the user message, each labelled C1, C2, ... and tagged"
    " with its filing's period_end_date.\n"
    "\n"
    "Grounding — hard constraint (ADR-0007 §4 carries over):\n"
    "- Use ONLY the supplied chunks. Your background knowledge about the company"
    " is off-limits, even when you are certain it is correct.\n"
    "- Cite the chunk label(s) each period's state rests on via chunk_ids.\n"
    "\n"
    "Change entries (ADR-0009 §3, §4, §5):\n"
    "- Group findings by dimension (e.g. 'Tariff risk', 'Data privacy'). For each"
    " entry, put the earlier period's stated position in period_a and the later"
    " period's in period_b, each citing its own chunk label(s).\n"
    "- Only emit an entry you can cite on BOTH sides — both periods must be"
    " grounded. If one period's chunks do not address a dimension, do not emit"
    " that entry; note the coverage gap in explanation instead.\n"
    "- Emit changed=false ONLY when both periods' cited text substantively states"
    " the same thing on the dimension — a genuine no-change finding requires both"
    " sides to actually address the dimension and say materially the same thing."
    " 'No change' is a legitimate finding; never manufacture a difference to"
    " appear useful, and never assert sameness the cited text does not show.\n"
    "- If the two periods' retrieved chunks address a dimension UNEVENLY — one"
    " side covers it and the other does not, or they cover different aspects so"
    " a like-for-like comparison is not grounded — do NOT emit the entry. Omit"
    " it and note the coverage gap in explanation instead.\n"
    "- change_description is for the factual description of an actual grounded"
    " difference (or genuine sameness). Coverage caveats like 'the retrieved"
    " chunk does not restate X' belong in explanation, never in"
    " change_description — if you find yourself caveating that you cannot"
    " compare, do not emit the entry.\n"
    "\n"
    "Factual/structural only (ADR-0009 §6):\n"
    "- change_description states WHAT differs. Label magnitude factually — for"
    " numeric changes, absolute and percent as stated in the filings.\n"
    "- Do NOT rank changes by importance, do NOT judge significance, do NOT frame"
    " anything as an opportunity or as attractive, and never give investment"
    " advice (ADR-0007 §3).\n"
    "\n"
    "explanation: 1-3 plain-text sentences on retrieval scope and limits only —"
    " never your reasoning process, drafting notes, or any HTML/markup."
)


class DraftPeriodState(BaseModel):
    """One period's side of a change entry, as the model emits it: a factual
    state plus the chunk labels grounding it. No URL, no date — those are
    stamped from retrieval during resolution."""

    state: str = Field(
        description=(
            "What this period's filing states on the dimension — factual,"
            " grounded in the cited chunks, plain text only."
        )
    )
    chunk_ids: list[str] = Field(default_factory=list)


class DraftChangeEntry(BaseModel):
    dimension: str
    changed: bool = Field(
        description=(
            "false when both periods state the same thing on this dimension —"
            " a legitimate, first-class finding, not a failure."
        )
    )
    period_a: DraftPeriodState  # earlier period
    period_b: DraftPeriodState  # later period
    change_description: str = Field(
        description=(
            "Factual statement of what differs between the periods (or that"
            " nothing does). No significance judgment, no reasoning process,"
            " no markup."
        )
    )


class ComparisonDraft(BaseModel):
    """Model-facing output shape for change detection."""

    entries: list[DraftChangeEntry] = Field(default_factory=list)
    explanation: str = Field(
        default="",
        description=(
            "Brief plain-text note (1-3 sentences) on what the retrieved chunks"
            " do and do not cover — retrieval scope and limits only. Never"
            " include your reasoning process, drafting notes, or self-"
            "corrections, and never HTML or markup."
        ),
    )


class PeriodState(BaseModel):
    """One period's side of a resolved change entry. period_end_date and
    source_url are stamped from the retrieved chunks and filings table
    (ADR-0009 §3) — never taken from model output."""

    state: str
    period_end_date: date
    source_url: str


class ChangeEntry(BaseModel):
    dimension: str
    changed: bool  # False = "no change on this dimension", both periods cited
    period_a: PeriodState  # earlier period
    period_b: PeriodState  # later period
    change_description: str


@dataclass(frozen=True)
class ComparisonResult:
    period_comparison: list[ChangeEntry]
    citations: dict[str, ChunkMatch]  # chunk label -> retrieved chunk
    warnings: list[QaWarning]  # first-class; never swallowed
    explanation: str


class ComparisonAnswerer(Protocol):
    def compare(
        self, question: str, chunks: dict[str, ChunkMatch], periods: Mapping[int, date]
    ) -> ComparisonDraft: ...


def build_comparison_prompt(
    question: str, chunks: dict[str, ChunkMatch], periods: Mapping[int, date]
) -> str:
    rendered = "\n\n".join(
        f'<chunk label="{label}" ticker="{match.ticker}" section="{match.section}"'
        f' period_end_date="{periods[match.filing_id].isoformat()}"'
        f' source_url="{match.source_url}">\n{match.content}\n</chunk>'
        for label, match in chunks.items()
    )
    return (
        f"Question: {question}\n\n"
        f"Evidence chunks across periods (the ONLY material you may use):\n\n{rendered}"
    )


def _resolve_side(
    dimension: str,
    side: DraftPeriodState,
    retrieved: dict[str, ChunkMatch],
    periods: Mapping[int, date],
) -> tuple[tuple[int, PeriodState] | None, list[QaWarning]]:
    """Stamp one period side from its citations, or explain why it can't be."""
    if not side.chunk_ids:
        return None, [
            QaWarning(
                kind="missing_period_citation",
                message=(
                    f"change entry {dimension!r} has a period side with no citation;"
                    " a comparison must cite both periods (ADR-0009 §4)"
                ),
            )
        ]
    unknown = [cid for cid in side.chunk_ids if cid not in retrieved]
    if unknown:
        return None, [
            QaWarning(
                kind="unknown_citation",
                message=(
                    f"change entry {dimension!r} cites {', '.join(unknown)}, which"
                    " is not in the retrieved set"
                ),
            )
        ]
    filing_ids = {retrieved[cid].filing_id for cid in side.chunk_ids}
    if len(filing_ids) != 1:
        return None, [
            QaWarning(
                kind="missing_period_citation",
                message=(
                    f"change entry {dimension!r} cites chunks from multiple periods"
                    " on one side; each side must ground exactly one period"
                ),
            )
        ]
    (filing_id,) = filing_ids
    if filing_id not in periods:
        return None, [
            QaWarning(
                kind="missing_period_citation",
                message=(f"change entry {dimension!r} cites a filing outside the compared set"),
            )
        ]
    source_url = retrieved[side.chunk_ids[0]].source_url
    return (
        filing_id,
        PeriodState(state=side.state, period_end_date=periods[filing_id], source_url=source_url),
    ), []


def resolve_change_entries(
    draft: ComparisonDraft,
    retrieved: dict[str, ChunkMatch],
    periods: Mapping[int, date],
) -> tuple[list[ChangeEntry], list[QaWarning]]:
    """Mechanical enforcement of ADR-0009 §4 plus the artifact guard.

    An entry that cannot cite both periods is NOT emitted (decision #4) and
    the drop is surfaced on the warnings channel, never silent. Leaked-
    reasoning artifacts in the free-text fields are flagged but the entry
    survives — same flag-not-strip posture as verify_answer.
    """
    entries: list[ChangeEntry] = []
    warnings: list[QaWarning] = []
    warnings.extend(_artifact_warnings("explanation", draft.explanation))
    for entry in draft.entries:
        for field_name, value in (
            ("change_description", entry.change_description),
            ("period_a state", entry.period_a.state),
            ("period_b state", entry.period_b.state),
        ):
            warnings.extend(_artifact_warnings(f"change entry {field_name}", value))
        resolved_a, side_warnings = _resolve_side(
            entry.dimension, entry.period_a, retrieved, periods
        )
        warnings.extend(side_warnings)
        resolved_b, side_warnings = _resolve_side(
            entry.dimension, entry.period_b, retrieved, periods
        )
        warnings.extend(side_warnings)
        if resolved_a is None or resolved_b is None:
            continue
        (filing_a, state_a), (filing_b, state_b) = resolved_a, resolved_b
        if filing_a == filing_b:
            warnings.append(
                QaWarning(
                    kind="missing_period_citation",
                    message=(
                        f"change entry {entry.dimension!r} cites the same period on"
                        " both sides; a comparison must cite two periods"
                        " (ADR-0009 §4)"
                    ),
                )
            )
            continue
        if state_a.period_end_date > state_b.period_end_date:
            # period_a is defined as the earlier period; each state keeps the
            # citations that ground it, only the slot assignment flips.
            state_a, state_b = state_b, state_a
        entries.append(
            ChangeEntry(
                dimension=entry.dimension,
                changed=entry.changed,
                period_a=state_a,
                period_b=state_b,
                change_description=entry.change_description,
            )
        )
    return entries, warnings


def detect_changes(
    engine: Engine,
    embedder: Embedder,
    answerer: ComparisonAnswerer,
    question: str,
    filing_ids: Sequence[int],
    limit: int = 8,
) -> ComparisonResult:
    """THE change-detection function (ADR-0009 decision #1). If a future ADR
    adds persistence for alerting/briefing, the store caches this function's
    output — one definition of a change in the system."""
    if not question.strip():
        raise ValueError("question must not be blank")
    unique_ids = list(dict.fromkeys(filing_ids))
    if len(unique_ids) < 2:
        raise ValueError("change detection needs at least two filings to compare")
    matches = balanced_semantic_search(engine, embedder, question, unique_ids, limit)
    if not matches:
        return ComparisonResult(
            period_comparison=[],
            citations={},
            warnings=[],
            explanation="No relevant chunks were retrieved for this comparison.",
        )
    with engine.connect() as conn:
        periods = {
            p.filing_id: p.period_end_date for p in Repository(conn).filing_periods(unique_ids)
        }
    chunks = {f"C{i}": match for i, match in enumerate(matches, start=1)}
    try:
        draft = answerer.compare(question, chunks, periods)
    except anthropic.APIError as exc:
        raise QaError(f"anthropic: {exc}") from exc
    entries, warnings = resolve_change_entries(draft, chunks, periods)
    return ComparisonResult(
        period_comparison=entries,
        citations=chunks,
        warnings=warnings,
        explanation=draft.explanation,
    )


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
