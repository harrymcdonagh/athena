"""Extraction repair for filings whose stored sections were corrupted by the
old section extractor. Derived-data repair ONLY: it touches filing_summaries
and filing_chunks rows for damaged sections and APPENDS a thesis snapshot —
companies rows, filings rows, and existing thesis_snapshots are never mutated
(thesis history is append-only by design; the migration 0001 trigger enforces
it, and the superseded snapshot honestly records what was believed at the
time).

The work-list is COMPUTED, never hardcoded: every stored filing is re-audited
by fetching its document from EDGAR, guarding filings.content_sha256 (a sha
mismatch means the EDGAR document changed since ingest — the filing is
reported and SKIPPED, because repairing from a different document would break
the audit chain), re-running the fixed extract_sections, and diffing each
section's fresh text against the stored filing_summaries.source_text. Only
differing sections are re-summarized; unaffected sections' stored summaries
are reused when the new thesis snapshot is composed. A repaired section's
chunks are deleted, which is what queues it for the existing embeddings
backfill — repair never embeds.

Reruns are idempotent (a repaired section diffs clean), one filing's failure
never aborts the run, and every audited filing ends in a known state:
clean / repaired (corrupted, in audit-only mode) / sha_mismatch /
failed-with-reason.

Run with: python -m apps.api.research.repair [--audit-only]
--audit-only prints the full diff report and writes NOTHING.
After a writing run, re-embed with: python -m apps.api.research.embeddings
"""

import argparse
import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx2 as httpx
from sqlalchemy import Engine, create_engine

from apps.api.config import get_settings
from apps.api.edgar.client import CompanyRef, EdgarClient, EdgarError, FilingRef
from apps.api.edgar.sections import SectionExtractionError, extract_sections
from apps.api.research.repository import RepairCandidate, Repository, StoredSummary
from apps.api.research.service import compose_thesis
from apps.api.research.summarizer import ClaudeSummarizer, Summarizer

_logger = logging.getLogger(__name__)

FailureCategory = Literal["parse_error", "rate_limited", "other"]

Status = Literal["clean", "corrupted", "repaired", "sha_mismatch", "failed"]


class DocumentFetcher(Protocol):
    def fetch_document(self, filing: FilingRef) -> str: ...


@dataclass(frozen=True)
class SectionDiff:
    section: str
    stored_chars: int
    fresh_chars: int


@dataclass(frozen=True)
class FilingAuditResult:
    ticker: str
    accession_number: str
    status: Status
    damaged: tuple[SectionDiff, ...] = ()
    category: FailureCategory | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RepairReport:
    audit_only: bool
    results: tuple[FilingAuditResult, ...]

    @property
    def audited(self) -> int:
        return len(self.results)

    @property
    def clean(self) -> int:
        return sum(1 for r in self.results if r.status == "clean")

    @property
    def corrupted(self) -> int:
        return sum(1 for r in self.results if r.status == "corrupted")

    @property
    def repaired(self) -> int:
        return sum(1 for r in self.results if r.status == "repaired")

    @property
    def sha_mismatches(self) -> int:
        return sum(1 for r in self.results if r.status == "sha_mismatch")

    @property
    def failures(self) -> tuple[FilingAuditResult, ...]:
        return tuple(r for r in self.results if r.status == "failed")


def _is_rate_limited(exc: BaseException | None) -> bool:
    while exc is not None:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
            return True
        exc = exc.__cause__
    return False


def _categorize(exc: Exception) -> FailureCategory:
    if _is_rate_limited(exc):
        return "rate_limited"
    if isinstance(exc, SectionExtractionError | EdgarError):
        return "parse_error"
    return "other"


def compose_repaired_thesis(
    company: CompanyRef, filing: FilingRef, summaries: dict[str, str]
) -> str:
    """The ingest-time thesis with a self-identifying repair title.

    Thesis history is append-only, so the repaired snapshot must be
    distinguishable from the extraction-corrupted original it supersedes —
    both would otherwise read "Initial thesis snapshot". Only the title block
    differs; the body stays byte-compatible with compose_thesis so ingest-time
    and repaired snapshots remain directly comparable."""
    thesis = compose_thesis(company, filing, summaries)
    initial_title = f"# Initial thesis snapshot: {company.name} ({company.ticker})"
    if not thesis.startswith(initial_title):
        # Fails the filing loudly instead of appending a mislabeled snapshot.
        raise RuntimeError(
            "compose_thesis no longer opens with the initial-snapshot title;"
            " update compose_repaired_thesis to match"
        )
    repaired_title = (
        f"# Repaired thesis snapshot: {company.name} ({company.ticker})\n"
        "\n"
        "_Supersedes an earlier snapshot composed from sections corrupted by a"
        " since-fixed extraction bug; recomposed from the repaired summaries."
        " The original remains in the append-only history._"
    )
    return repaired_title + thesis[len(initial_title) :]


def _filing_ref(candidate: RepairCandidate) -> FilingRef:
    return FilingRef(
        accession_number=candidate.accession_number,
        form_type=candidate.form_type,
        filing_date=candidate.filing_date,
        period_end_date=candidate.period_end_date,
        filing_url=candidate.filing_url,
    )


def run_repair(
    edgar: DocumentFetcher,
    summarizer: Summarizer | None,
    engine: Engine,
    *,
    audit_only: bool = False,
    delay_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> RepairReport:
    """Re-audit every stored filing and (unless audit_only) repair the damaged
    ones, skip-and-continue per filing, accounting for each in the report.

    Every filing is one EDGAR document fetch, so the batch politeness delay
    applies between every pair of filings regardless of outcome.
    """
    if not audit_only and summarizer is None:
        raise ValueError("a summarizer is required unless audit_only")
    with engine.connect() as conn:
        repo = Repository(conn)
        candidates = repo.filings_for_repair_audit()
        stored_by_filing = {c.filing_id: repo.stored_summaries(c.filing_id) for c in candidates}
    results: list[FilingAuditResult] = []
    for i, candidate in enumerate(candidates):
        if i > 0 and delay_seconds > 0:
            sleep(delay_seconds)
        result = _audit_one(
            edgar,
            summarizer,
            engine,
            candidate,
            stored_by_filing[candidate.filing_id],
            audit_only=audit_only,
        )
        _logger.info(
            "%s %s: %s%s",
            candidate.ticker,
            candidate.accession_number,
            result.status,
            f" ({result.reason})" if result.reason else "",
        )
        results.append(result)
    return RepairReport(audit_only=audit_only, results=tuple(results))


def _audit_one(
    edgar: DocumentFetcher,
    summarizer: Summarizer | None,
    engine: Engine,
    candidate: RepairCandidate,
    stored: dict[str, StoredSummary],
    *,
    audit_only: bool,
) -> FilingAuditResult:
    def failed(exc: Exception) -> FilingAuditResult:
        return FilingAuditResult(
            ticker=candidate.ticker,
            accession_number=candidate.accession_number,
            status="failed",
            category=_categorize(exc),
            reason=f"{type(exc).__name__}: {exc}",
        )

    filing = _filing_ref(candidate)
    try:
        html = edgar.fetch_document(filing)
    except Exception as exc:
        return failed(exc)

    fetched_sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
    if fetched_sha != candidate.content_sha256:
        return FilingAuditResult(
            ticker=candidate.ticker,
            accession_number=candidate.accession_number,
            status="sha_mismatch",
            reason=(
                f"stored content_sha256 {candidate.content_sha256[:12]}… does not match the"
                f" fetched document ({fetched_sha[:12]}…) — the EDGAR document changed since"
                " ingest; repairing from it would break the audit chain, so the filing is"
                " skipped"
            ),
        )

    try:
        fresh = extract_sections(html)
    except SectionExtractionError as exc:
        return failed(exc)

    # Only fresh-minus-stored needs guarding: extract_sections returns the
    # full fixed section set or raises, so stored-minus-fresh is empty by
    # construction and every stored section is accounted for in `merged`.
    missing = sorted(set(fresh) - set(stored))
    if missing:
        # A filing with fewer summary rows than extracted sections was never
        # written by the ingest path; refuse to guess and report it instead.
        return failed(
            RuntimeError(f"stored summaries missing section(s) {missing}; unexpected state")
        )
    damaged = tuple(
        SectionDiff(
            section=section,
            stored_chars=len(stored[section].source_text),
            fresh_chars=len(fresh[section]),
        )
        for section in fresh
        if stored[section].source_text != fresh[section]
    )
    if not damaged:
        return FilingAuditResult(
            ticker=candidate.ticker,
            accession_number=candidate.accession_number,
            status="clean",
        )
    if audit_only:
        return FilingAuditResult(
            ticker=candidate.ticker,
            accession_number=candidate.accession_number,
            status="corrupted",
            damaged=damaged,
        )

    assert summarizer is not None  # guaranteed by run_repair
    # Summarize BEFORE the write transaction opens (mirroring the ingest
    # path), so a summarizer failure writes nothing for this filing.
    try:
        repaired_summaries = {
            diff.section: summarizer.summarize(
                diff.section, fresh[diff.section], stored[diff.section].source_url
            )
            for diff in damaged
        }
    except Exception as exc:
        return failed(exc)

    # Unaffected sections reuse their stored summaries; only damaged sections
    # carry the new ones into the appended snapshot. ADR-0014: an unaffected
    # section may be PENDING (summary NULL) — a valid state, not an error. A
    # thesis is composed only when every section has a summary; if any is still
    # pending, the repaired source_text is written but the thesis stays deferred
    # to first demand (summarize_on_demand), not re-introduced eagerly here.
    merged: dict[str, str | None] = {
        section: repaired_summaries.get(section, stored[section].summary) for section in fresh
    }
    complete = {section: value for section, value in merged.items() if value is not None}
    company = CompanyRef(ticker=candidate.ticker, cik=candidate.cik, name=candidate.company_name)
    thesis = (
        compose_repaired_thesis(company, filing, complete) if len(complete) == len(merged) else None
    )
    try:
        with engine.begin() as conn:
            repo = Repository(conn)
            for diff in damaged:
                updated = repo.update_summary(
                    candidate.filing_id,
                    diff.section,
                    summary=repaired_summaries[diff.section],
                    source_text=fresh[diff.section],
                    model=summarizer.model,
                )
                if updated != 1:
                    raise RuntimeError(
                        f"expected exactly one summary row for filing {candidate.filing_id}"
                        f" section {diff.section!r}, updated {updated}"
                    )
                repo.delete_chunks(candidate.filing_id, diff.section)
            if thesis is not None:
                repo.insert_thesis_snapshot(candidate.company_id, candidate.filing_id, thesis)
    except Exception as exc:
        return failed(exc)
    return FilingAuditResult(
        ticker=candidate.ticker,
        accession_number=candidate.accession_number,
        status="repaired",
        damaged=damaged,
    )


def _damage_line(result: FilingAuditResult) -> str:
    details = ", ".join(
        f"{d.section} (stored {d.stored_chars} chars → fresh {d.fresh_chars} chars)"
        for d in result.damaged
    )
    return f"  {result.ticker} {result.accession_number}: {details}"


def format_report(report: RepairReport) -> str:
    corrupted_label = (
        f"{report.corrupted} corrupted (would repair)"
        if report.audit_only
        else f"{report.repaired} repaired"
    )
    lines = [
        f"extraction repair ({'audit-only' if report.audit_only else 'repair'}):"
        f" {report.audited} audited — {corrupted_label}, {report.clean} clean,"
        f" {report.sha_mismatches} sha_mismatch, {len(report.failures)} failed"
    ]
    flagged = [r for r in report.results if r.status in ("corrupted", "repaired")]
    if flagged:
        lines.append("corrupted filings:" if report.audit_only else "repaired filings:")
        lines += [_damage_line(r) for r in flagged]
    mismatched = [r for r in report.results if r.status == "sha_mismatch"]
    if mismatched:
        lines.append("sha mismatches (EDGAR document changed since ingest; skipped):")
        lines += [f"  {r.ticker} {r.accession_number}: {r.reason}" for r in mismatched]
    if report.failures:
        lines.append("failed filings:")
        lines += [
            f"  {r.ticker} {r.accession_number}: {r.category} — {r.reason}" for r in report.failures
        ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-audit stored filing sections against the fixed extractor and repair"
        " corrupted ones (derived data only)."
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="print the full diff report and write nothing",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.sec_edgar_user_agent:
        raise SystemExit("SEC_EDGAR_USER_AGENT is not set; aborting without contacting EDGAR.")
    summarizer: Summarizer | None = None
    if not args.audit_only:
        if not settings.anthropic_api_key:
            raise SystemExit("ANTHROPIC_API_KEY is not set; aborting without repairing anything.")
        summarizer = ClaudeSummarizer(api_key=settings.anthropic_api_key)
    engine = create_engine(settings.database_url)
    edgar = EdgarClient(user_agent=settings.sec_edgar_user_agent)
    report = run_repair(
        edgar,
        summarizer,
        engine,
        audit_only=args.audit_only,
        delay_seconds=settings.edgar_batch_delay_seconds,
    )
    print(format_report(report))
    if not report.audit_only and report.repaired:
        print(
            "reminder: run `python -m apps.api.research.embeddings` to re-embed repaired sections"
        )
    if report.failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
