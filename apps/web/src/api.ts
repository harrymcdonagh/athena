// API client for the local Athena FastAPI backend.
//
// Base URL is configurable via VITE_API_BASE_URL (e.g. in apps/web/.env.local);
// defaults to the local dev API.
export const API_BASE: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";

// ---------------------------------------------------------------------------
// Response types — mirrors of the Pydantic response models in
// apps/api/research/router.py and qa.py. Fields listed here are exactly the
// fields the API returns; nothing is invented on the frontend.
// ---------------------------------------------------------------------------

export interface FindPassage {
  snippet: string;
  source_url: string;
  similarity: number;
}

export interface FindCompanyMatch {
  ticker: string;
  company_name: string;
  // How well the filing's TEXT matched the query (best passage's cosine
  // similarity). A retrieval fact — never a judgment about the company.
  match_strength: number;
  passages: FindPassage[];
}

export interface FindResponse {
  query: string;
  matches: FindCompanyMatch[];
}

export interface CompanyListItem {
  ticker: string;
  company_name: string;
  filing_count: number;
  latest_period_end_date: string;
  has_multiple_filings: boolean;
}

export interface CompaniesResponse {
  companies: CompanyListItem[];
}

export interface SearchResult {
  content: string;
  source_url: string;
  ticker: string;
  section: string;
  filing_id: number;
  chunk_index: number;
  // pgvector cosine distance; the backend's own similarity convention is
  // similarity = 1 - distance (see apps/api/research/find.py).
  distance: number;
}

export interface Claim {
  text: string;
  chunk_ids: string[];
}

export type QaMode = "direct" | "two_sided" | "insufficient_evidence" | "no_prior_period";

export interface QaAnswer {
  mode: QaMode;
  claims: Claim[];
  bull: Claim[];
  bear: Claim[];
  what_changed: Claim[];
  verdict_note: string;
  explanation: string;
}

export interface QaWarning {
  kind: string;
  message: string;
}

export interface QaResponse {
  answer: QaAnswer;
  citations: Record<string, SearchResult>;
  warnings: QaWarning[];
}

export interface PeriodState {
  state: string;
  period_end_date: string;
  source_url: string;
}

export interface ChangeEntry {
  dimension: string;
  changed: boolean; // false = "no change on this dimension" — a real finding
  period_a: PeriodState; // earlier period
  period_b: PeriodState; // later period
  change_description: string;
}

export interface ComparisonResponse {
  period_comparison: ChangeEntry[];
  citations: Record<string, SearchResult>;
  warnings: QaWarning[];
  explanation: string;
}

export function isComparison(r: QaResponse | ComparisonResponse): r is ComparisonResponse {
  return "period_comparison" in r;
}

// ---------------------------------------------------------------------------
// Request layer — every call reports {status, latency} to subscribers so the
// top bar can show a live readout of real fetch timings.
// ---------------------------------------------------------------------------

export interface RequestMeta {
  status: number | null; // null = network-level failure (or in flight)
  statusText: string;
  ms: number;
  pending: boolean;
}

type MetaListener = (meta: RequestMeta) => void;
const metaListeners = new Set<MetaListener>();

export function onRequestMeta(listener: MetaListener): () => void {
  metaListeners.add(listener);
  return () => {
    metaListeners.delete(listener);
  };
}

function emitMeta(meta: RequestMeta): void {
  for (const listener of metaListeners) listener(meta);
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
  ) {
    super(message);
  }
}

async function detailFrom(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      return typeof detail === "string" ? detail : JSON.stringify(detail);
    }
  } catch {
    // non-JSON error body — fall through to the generic message
  }
  return "";
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const started = performance.now();
  emitMeta({ status: null, statusText: "", ms: 0, pending: true });

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, init);
  } catch {
    emitMeta({
      status: null,
      statusText: "unreachable",
      ms: Math.round(performance.now() - started),
      pending: false,
    });
    throw new ApiError(
      `could not reach the API at ${API_BASE} — check that it is running and that CORS allows this origin`,
      null,
    );
  }

  emitMeta({
    status: res.status,
    statusText: res.statusText || (res.ok ? "OK" : ""),
    ms: Math.round(performance.now() - started),
    pending: false,
  });

  if (!res.ok) {
    const detail = await detailFrom(res);
    const message =
      res.status === 422
        ? `query rejected — ${detail || "invalid input"}`
        : res.status === 502
          ? `upstream failure — ${detail || "the embedding or answer service is unavailable"}`
          : detail || `request failed with status ${res.status}`;
    throw new ApiError(message, res.status);
  }

  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Filing-meta derivation. The find response carries no form type / fiscal
// year / accession fields, so the only honest filing meta is what the SEC
// source_url itself encodes: .../Archives/edgar/data/{cik}/{accession}/{doc}.
// Anything not derivable from the URL is omitted, never fabricated.
// ---------------------------------------------------------------------------

export function accessionFromUrl(url: string): string | null {
  const match = url.match(/\/(\d{18})\//);
  if (!match) return null;
  const raw = match[1];
  return `${raw.slice(0, 10)}-${raw.slice(10, 12)}-${raw.slice(12)}`;
}

export function documentFromUrl(url: string): string | null {
  try {
    return new URL(url).pathname.split("/").pop() || null;
  } catch {
    return null;
  }
}
