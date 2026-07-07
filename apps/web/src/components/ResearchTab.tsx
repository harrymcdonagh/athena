import { useEffect, useState } from "react";
import {
  ApiError,
  isComparison,
  request,
  type Claim,
  type CompaniesResponse,
  type CompanyListItem,
  type ComparisonResponse,
  type QaResponse,
  type QaWarning,
  type SearchResult,
} from "../api";
import { InlineError, Loading, SecLink } from "./QueryRow";

type QaResult = QaResponse | ComparisonResponse;

export function ResearchTab() {
  const [companies, setCompanies] = useState<CompanyListItem[] | null>(null);
  const [companiesError, setCompaniesError] = useState(false);
  const [ticker, setTicker] = useState("");
  const [question, setQuestion] = useState("");
  const [compare, setCompare] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<QaResult | null>(null);

  useEffect(() => {
    let cancelled = false;
    request<CompaniesResponse>("/research/companies")
      .then((response) => {
        if (!cancelled) setCompanies(response.companies);
      })
      .catch(() => {
        // Picker fallback: free-text ticker input, no hardcoded company list.
        if (!cancelled) setCompaniesError(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const selectedCompany = companies?.find((c) => c.ticker === ticker) ?? null;

  async function run() {
    const q = question.trim();
    if (q.length < 2) {
      setError("enter a question of at least 2 characters");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const response = await request<QaResult>("/research/qa", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: q,
          ticker: ticker.trim() ? ticker.trim().toUpperCase() : null,
          compare,
        }),
      });
      setResult(response);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "unexpected error — see console");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="research">
      <div className="query-row research-controls">
        <span className="prompt-label">research&gt;</span>
        {companiesError ? (
          <input
            className="ticker-input"
            type="text"
            value={ticker}
            placeholder="ticker (optional)"
            spellCheck={false}
            onChange={(event) => setTicker(event.target.value.toUpperCase())}
          />
        ) : (
          <select
            className="ticker-select"
            value={ticker}
            onChange={(event) => {
              setTicker(event.target.value);
              if (!event.target.value) setCompare(false);
            }}
            disabled={companies === null}
          >
            <option value="">
              {companies === null ? "loading companies…" : "all companies"}
            </option>
            {companies?.map((company) => (
              <option key={company.ticker} value={company.ticker}>
                {company.ticker} — {company.company_name}
              </option>
            ))}
          </select>
        )}
        <label className={`compare-toggle${ticker ? "" : " compare-toggle-disabled"}`}>
          <input
            type="checkbox"
            checked={compare}
            disabled={!ticker}
            onChange={(event) => setCompare(event.target.checked)}
          />
          <span>[{compare ? "x" : " "}] compare across years</span>
        </label>
        <span className="endpoint">POST /research/qa</span>
      </div>

      <div className="query-row">
        <label className="prompt">
          <span className="prompt-label">q&gt;</span>
          <span className="prompt-input-wrap">
            <input
              className="prompt-input"
              type="text"
              value={question}
              placeholder="ask a question of the stored filings…"
              spellCheck={false}
              autoComplete="off"
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !loading) run();
              }}
            />
          </span>
        </label>
      </div>

      {companiesError && (
        <p className="hint">company list unavailable — enter a ticker by hand, or leave blank</p>
      )}
      {compare && selectedCompany && !selectedCompany.has_multiple_filings && (
        <p className="hint">
          {selectedCompany.ticker} has one stored filing — compare will report that there is no
          prior period
        </p>
      )}

      {error && <InlineError message={error} />}
      {loading && <Loading label={compare ? "comparing periods…" : "answering from filings…"} />}

      {!loading && result && (
        <div className="qa-result">
          {isComparison(result) ? <ComparisonView result={result} /> : <AnswerView result={result} />}
          <Warnings warnings={result.warnings} />
          <Citations citations={result.citations} />
        </div>
      )}

      {!loading && !result && !error && (
        <p className="empty-state">
          pick a company (or all), ask a question, press enter — answers cite the filings they
          rest on
        </p>
      )}
    </div>
  );
}

// --- grounded answer (compare off) -----------------------------------------

function AnswerView({ result }: { result: QaResponse }) {
  const { answer } = result;
  return (
    <div>
      {answer.mode === "direct" && (
        <ClaimList heading="ANSWER" claims={answer.claims} citations={result.citations} />
      )}
      {answer.mode === "two_sided" && (
        <>
          <div className="two-sided">
            <ClaimList heading="BULL — EVIDENCE" claims={answer.bull} citations={result.citations} />
            <ClaimList heading="BEAR — EVIDENCE" claims={answer.bear} citations={result.citations} />
          </div>
          {answer.what_changed.length > 0 && (
            <ClaimList
              heading="WHAT CHANGED"
              claims={answer.what_changed}
              citations={result.citations}
            />
          )}
          {answer.verdict_note && <p className="verdict-note">{answer.verdict_note}</p>}
        </>
      )}
      {(answer.mode === "insufficient_evidence" || answer.mode === "no_prior_period") && (
        <div className="notice">
          <p className="pane-heading">
            {answer.mode === "insufficient_evidence" ? "INSUFFICIENT EVIDENCE" : "NO PRIOR PERIOD"}
          </p>
          <p>{answer.explanation || "the stored filings do not cover this question."}</p>
        </div>
      )}
      {answer.mode !== "insufficient_evidence" &&
        answer.mode !== "no_prior_period" &&
        answer.explanation && <p className="dim explanation">{answer.explanation}</p>}
    </div>
  );
}

function ClaimList({
  heading,
  claims,
  citations,
}: {
  heading: string;
  claims: Claim[];
  citations: Record<string, SearchResult>;
}) {
  return (
    <div className="claim-list">
      <p className="pane-heading">{heading}</p>
      <ul>
        {claims.map((claim, index) => (
          <li className="claim" key={index}>
            <span>{claim.text}</span>{" "}
            {claim.chunk_ids.map((id) => {
              const cited = citations[id];
              return cited ? (
                <a
                  key={id}
                  className="citation-chip"
                  href={cited.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={`${cited.ticker} · ${cited.section} — view in SEC filing`}
                >
                  [{id}]
                </a>
              ) : (
                <span key={id} className="citation-chip citation-chip-dead" title="citation not in response">
                  [{id}]
                </span>
              );
            })}
          </li>
        ))}
      </ul>
    </div>
  );
}

// --- period comparison (compare on) -----------------------------------------

function ComparisonView({ result }: { result: ComparisonResponse }) {
  return (
    <div>
      {result.explanation && <p className="explanation">{result.explanation}</p>}
      {result.period_comparison.map((entry) => (
        <article className="change-entry" key={entry.dimension}>
          <header className="change-entry-header">
            <span className="passage-label">{entry.dimension.toUpperCase()}</span>
            <span className={entry.changed ? "marker marker-changed" : "marker"}>
              {entry.changed ? "CHANGED" : "NO CHANGE ON THIS DIMENSION"}
            </span>
          </header>
          <div className="period-grid">
            {[entry.period_a, entry.period_b].map((period, index) => (
              <div className="period-cell" key={index}>
                <p className="dim">
                  {index === 0 ? "earlier" : "later"} · period ending {period.period_end_date}
                </p>
                <p>{period.state}</p>
                <SecLink href={period.source_url} />
              </div>
            ))}
          </div>
          <p className="change-description">{entry.change_description}</p>
        </article>
      ))}
    </div>
  );
}

// --- shared blocks -----------------------------------------------------------

function Warnings({ warnings }: { warnings: QaWarning[] }) {
  if (warnings.length === 0) return null;
  return (
    <ul className="warnings">
      {warnings.map((warning, index) => (
        <li key={index}>
          ! {warning.kind} — {warning.message}
        </li>
      ))}
    </ul>
  );
}

function Citations({ citations }: { citations: Record<string, SearchResult> }) {
  const entries = Object.entries(citations);
  if (entries.length === 0) return null;
  return (
    <div className="citations">
      <p className="pane-heading">CITATIONS</p>
      <ul>
        {entries.map(([label, citation]) => (
          <li key={label}>
            <span className="citation-chip-static">[{label}]</span>{" "}
            <span className="ticker">{citation.ticker}</span>{" "}
            <span className="dim">{citation.section}</span> <SecLink href={citation.source_url} />
          </li>
        ))}
      </ul>
    </div>
  );
}
