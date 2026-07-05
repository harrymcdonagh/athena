# 0006 — Semantic Search: Voyage AI Embeddings over Filing Sections

**Status:** Accepted

## Context

Phase 2 adds semantic search over the stored 10-K corpus. ADR-0005 enabled
pgvector in anticipation; this ADR is the required written plan before any
schema or database code. Anthropic has no first-party embedding model and
recommends Voyage AI, which also offers finance-domain models suited to SEC
filings. The corpus is 4 filings today but will grow. 10-K sections are long
and heavily cross-referential (Item 7 routinely refers back to Item 1A and
Item 8 — the source of several recent extraction bugs), which bears directly
on the chunking choice.

## Decision

1. **Provider: Voyage AI.** This adds a new external runtime dependency and a
   new secret, `VOYAGE_API_KEY`, which lives in `.env` (git-ignored) and is
   documented by name only in `.env.example`, per existing convention.

2. **Chunking: contextualized-chunk model (`voyage-context-4`).** Two
   options were evaluated:
   - *(a) Self-managed:* split sections into ~800-token windows, embed each
     independently. Simple, provider-portable, but each chunk loses the
     surrounding document context — a real cost in 10-Ks, where a chunk's
     meaning often depends on material elsewhere in the filing.
   - *(b) Contextualized:* send whole sections; the model chunks and embeds
     each piece with full-document context. Better retrieval on long
     cross-referential documents, at the cost of tighter coupling to Voyage's
     API and chunking behaviour.

   We choose (b): retrieval quality on exactly our document type outweighs
   portability, and it removes a chunking implementation we would otherwise
   own and tune. The coupling risk is bounded — chunks and their source text
   are stored in our schema, so switching providers means re-embedding, not
   re-ingesting (see Consequences).

3. **Model and dimension: `voyage-context-4` at 1024 dimensions** (its
   default output size). The pgvector column is therefore `vector(1024)`.
   Each chunk row records the embedding model name and dimension so a future
   model change is detectable and migratable. Filing content is embedded with
   Voyage's `document` input type and search queries with the `query` input
   type — same model and dimension on both sides, so document and query
   vectors share one comparable space.

4. **Auditability (hard constraint):** every chunk row MUST carry
   `filing_id`, `section`, and `source_url`, so the
   summary → chunk → document → EDGAR URL chain survives retrieval. Any
   retrieved chunk is traceable to its exact source with `psql` alone, per
   ADR-0005.

5. **Index: HNSW** (supported in pgvector 0.8.4). IVFFlat requires training
   on existing data to build its lists and degrades as the corpus drifts from
   that sample; HNSW needs no training step, handles incremental inserts
   well, and gives better recall/latency at our expected scale. At 4 filings
   any index works — this is choosing the right default for growth, not
   optimization.

## Consequences

- New runtime dependency (Voyage AI) and new secret (`VOYAGE_API_KEY`); an
  outage or API change at Voyage degrades search, not ingestion or summaries.
- Embedding costs are per-token at ingestion time only; queries embed a
  single short string. At current corpus size this is negligible, and cost
  scales linearly with filings ingested.
- Changing embedding model (or provider) later requires re-embedding every
  chunk, since vectors from different models are not comparable. Stored
  section text makes this a batch job, not a re-ingestion; the recorded
  model name per row makes partial migration states visible.
- The per-chunk model + dimension tagging is what enables a future
  *incremental* re-embed (rows migrate model-by-model, mixed states are
  queryable) rather than a big-bang migration.
- HNSW exposes tuning knobs — `m` / `ef_construction` at index build time and
  `ef_search` at query time — noted here for future recall tuning; the
  defaults are fine at current scale.
- Coupling to Voyage's contextualized-chunking API means chunk boundaries
  are theirs, not ours; if we ever need custom boundaries we fall back to
  option (a) and re-embed.
