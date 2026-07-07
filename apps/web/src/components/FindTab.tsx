import { useState } from "react";
import {
  accessionFromUrl,
  ApiError,
  documentFromUrl,
  request,
  type FindCompanyMatch,
  type FindResponse,
} from "../api";
import { InlineError, Loading, QueryRow, SecLink } from "./QueryRow";

export function FindTab() {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<FindResponse | null>(null);
  const [selected, setSelected] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    const q = query.trim();
    if (q.length < 2) {
      setError("enter a query of at least 2 characters");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const response = await request<FindResponse>(`/research/find?q=${encodeURIComponent(q)}`);
      setResult(response);
      setSelected(0);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "unexpected error — see console");
    } finally {
      setLoading(false);
    }
  }

  const matches = result?.matches ?? [];
  const current: FindCompanyMatch | undefined = matches[selected];

  return (
    <div className="find">
      <QueryRow
        prompt="find>"
        endpoint="GET /research/find"
        value={query}
        onChange={setQuery}
        onSubmit={run}
        placeholder="which companies discuss …"
        busy={loading}
      />

      {error && <InlineError message={error} />}
      {loading && <Loading label="querying filings…" />}

      {!loading && result && matches.length === 0 && (
        <p className="empty-state">
          no filings matched “{result.query}” — try broader wording.
        </p>
      )}

      {!loading && matches.length > 0 && current && (
        <div className="find-panes">
          <div className="find-left">
            <p className="pane-heading">
              {matches.length} {matches.length === 1 ? "MATCH" : "MATCHES"} · ORDERED BY QUERY
              RELEVANCE
            </p>
            <ul className="company-list">
              {matches.map((match, index) => (
                <li key={match.ticker}>
                  <button
                    className={`company-row${index === selected ? " company-row-active" : ""}`}
                    onClick={() => setSelected(index)}
                  >
                    <span className="company-row-name">
                      <span className="ticker">{match.ticker}</span>
                      <span className="company-name">{match.company_name}</span>
                    </span>
                    <span className="match-bar-track">
                      <span
                        className="match-bar"
                        style={{ width: `${Math.round(match.match_strength * 100)}%` }}
                      />
                    </span>
                    <span className="company-row-meta">
                      <span>match {match.match_strength.toFixed(2)}</span>
                      <span className="dim">
                        {match.passages.length} cited passage
                        {match.passages.length === 1 ? "" : "s"}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            <p className="footnote">
              match = how well the filing's text matched your query. A retrieval fact — not a
              judgment about the company.
            </p>
          </div>

          <div className="find-right">
            <header className="detail-header">
              <p>
                <span className="ticker ticker-lg">{current.ticker}</span>
                <span className="company-name">{current.company_name}</span>
              </p>
              <FilingMeta match={current} />
            </header>
            {current.passages.map((passage, index) => (
              <article className="passage-card" key={`${current.ticker}-${index}`}>
                <header className="passage-card-header">
                  <span className="passage-label">PASSAGE {index + 1}</span>
                  <span className="dim">similarity {passage.similarity.toFixed(2)}</span>
                </header>
                <blockquote className="snippet">{passage.snippet.trim()}</blockquote>
                <SecLink href={passage.source_url} />
              </article>
            ))}
          </div>
        </div>
      )}

      {!loading && !result && !error && (
        <p className="empty-state">
          type a query and press enter — e.g. <span className="dim">data center power
          constraints</span>
        </p>
      )}
    </div>
  );
}

// The find response carries no form-type / fiscal-year / accession fields, so
// this shows only what the SEC source_url itself encodes: accession number and
// primary document name. Nothing else is derivable, so nothing else is shown.
function FilingMeta({ match }: { match: FindCompanyMatch }) {
  const urls = [...new Set(match.passages.map((p) => p.source_url))];
  const parts = urls.flatMap((url) => {
    const accession = accessionFromUrl(url);
    const doc = documentFromUrl(url);
    return accession ? [`accession ${accession}${doc ? ` · ${doc}` : ""}`] : [];
  });
  if (parts.length === 0) return null;
  return (
    <p className="dim filing-meta">
      {parts.map((part) => (
        <span key={part}>{part}</span>
      ))}
    </p>
  );
}
