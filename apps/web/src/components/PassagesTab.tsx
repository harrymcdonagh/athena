import { useState } from "react";
import { ApiError, request, type SearchResult } from "../api";
import { InlineError, Loading, QueryRow, SecLink } from "./QueryRow";

export function PassagesTab() {
  const [query, setQuery] = useState("");
  const [ticker, setTicker] = useState("");
  const [results, setResults] = useState<SearchResult[] | null>(null);
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
      const params = new URLSearchParams({ q });
      if (ticker.trim()) params.set("ticker", ticker.trim().toUpperCase());
      const response = await request<SearchResult[]>(`/research/search?${params}`);
      setResults(response);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "unexpected error — see console");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="passages">
      <QueryRow
        prompt="search>"
        endpoint="GET /research/search"
        value={query}
        onChange={setQuery}
        onSubmit={run}
        placeholder="semantic search across stored filing text…"
        busy={loading}
      >
        <input
          className="ticker-input"
          type="text"
          value={ticker}
          placeholder="ticker (optional)"
          spellCheck={false}
          onChange={(event) => setTicker(event.target.value.toUpperCase())}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !loading) run();
          }}
        />
      </QueryRow>

      {error && <InlineError message={error} />}
      {loading && <Loading label="searching chunks…" />}

      {!loading && results && results.length === 0 && (
        <p className="empty-state">no passages matched — try broader wording.</p>
      )}

      {!loading && results && results.length > 0 && (
        <div className="passage-list">
          <p className="pane-heading">
            {results.length} {results.length === 1 ? "PASSAGE" : "PASSAGES"} · ORDERED BY QUERY
            RELEVANCE
          </p>
          {results.map((chunk) => (
            <article className="passage-card" key={`${chunk.filing_id}-${chunk.chunk_index}`}>
              <header className="passage-card-header">
                <span>
                  <span className="ticker">{chunk.ticker}</span>
                  <span className="dim"> · {chunk.section}</span>
                </span>
                {/* the API returns cosine distance; the backend's own convention
                    is similarity = 1 - distance (find.py) */}
                <span className="dim">similarity {(1 - chunk.distance).toFixed(2)}</span>
              </header>
              <blockquote className="snippet">{chunk.content.trim()}</blockquote>
              <SecLink href={chunk.source_url} />
            </article>
          ))}
        </div>
      )}

      {!loading && !results && !error && (
        <p className="empty-state">
          raw semantic search over stored filing chunks — no synthesis, just the text and its
          source
        </p>
      )}
    </div>
  );
}
