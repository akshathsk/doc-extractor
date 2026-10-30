# comp-doc-extractor

Give it a **trader ID**, and agents read every contract that trader has (base agreement, amendments, side letters,
memos). They return the trader's **current compensation terms as a tree**: term → sub-term → value. Every value cites
its document, section, page and exact quote, and shows its history across documents. The same data serves a
**chatbot**, a **cross-trader term finder** and a **drafting agent**. A separate **Dev Console** shows every step and
everything stored.

All contracts in `data/synth/` are synthetic. Nothing here is real.

## Status (2026-10-30)

| Area | State |
| --- | --- |
| Core system (ingest, extraction, review, chat, drafting, UIs, API, MCP) | Built; verified live with OpenAI on the small set (all values correct) before the scaling work |
| Scaling build (Parts 1-5 of [docs/scaling-plan.md](docs/scaling-plan.md)) | Built and tested **offline**; live re-run pending (the OpenAI account needs credits) |
| Tests | 29 passing (`uv run pytest`, no API key needed) |
| Data | Synthetic only; scores are optimistic until checked on real, redacted contracts |
| Not built yet | Multi-machine conversion queue, background re-enrichment jobs, tagging feedback loop, cheaper-model Doc Readers, Word auto-numbering, LLM heading repair |

## Quick start

```bash
uv sync                                         # Python 3.12; first Docling run downloads its models
cp .env.example .env                            # add OPENAI_API_KEY, or set COMPDOC_PROVIDER=offline
uv run python data/synth/generate.py            # small set: 26 contracts, 10 traders, gold labels
uv run python data/synth/generate_large.py      # large set: ~200-page agreements + amendments, 2 traders
uv run compdoc ingest                           # Docling → sections → enrichment → embeddings (all cached)
uv run compdoc ingest --source data/synth/out_large

uv run compdoc extract T-1003                   # term tree for one trader
uv run compdoc extract T-1004                   # same-date conflict → pauses for human review
uv run compdoc review <run_id> --pick '<path>=<finding_id>' --reviewer me
uv run compdoc chat "When did T-1003's deferral change?"
uv run compdoc find Clawback                    # which documents have a clawback clause
uv run compdoc draft T-1003 "Raise payout to 18% and move to 4 year vesting"

uv run streamlit run ui/app.py --server.port 8501          # business UI
uv run streamlit run devconsole/app.py --server.port 8502  # Dev Console
uv run uvicorn compdoc.api:app --port 8000                 # REST API (X-Caller header = identity)
uv run python -m compdoc.mcp_server                        # MCP server (stdio)
```

**Providers:** `COMPDOC_PROVIDER=openai` (default: `gpt-5.5` for agents, `gpt-5.4-mini` for enrichment), `anthropic`
or `offline`. In offline mode every agent uses a rule-based policy that calls **the same tools**, so tests, traces
and UIs work without an API key. Offline rules can't read reworded clauses, though.

## How it works

```
INGEST  (once per document; cached by file hash and section content)
  source DB row → Docling (layout, tables, OCR) → section builder → QC → section text store
                → enrichment (LLM map-reduce: summaries, term tags, article + document summaries)
                → local embeddings (semantic search)

EXTRACTION  (LangGraph graph, checkpointed; trader_id in, versioned term tree out)
  orchestrator → coverage router (code) → Doc Readers in parallel (one per document / term group)
              → coverage check (re-queue gaps once) → evidence ledger
              → reconciler (date rules in code; LLM only on conflicts)
              → human review (pause) if conflicts remain → summarizer → publish

CHAT  router → Trader Q&A | Term Finder | Drafting, answers with citations
```

1. **Docling** turns PDFs (including scans) and DOCX into items with page and position. Large PDFs can optionally be
   split across workers on several machines.
2. **The section builder** rebuilds the clause tree. It handles `ARTICLE IV`, `Section 4.02`, `4.2.1`, `(a)(i)`,
   `Schedule A`, recitals, run-in headings, tables of contents and running headers, and it repairs lost heading numbers.
3. **The text store** saves each section's text and a map from character positions to Docling item, page and box.
   Quote checks and citations use it, never the raw Docling files.
4. **Enrichment** summarises and tags sections in parallel token-sized batches. It builds article and document
   summaries, and finds the effective date and period limits with verified citations. Unchanged sections are reused.
5. **The coverage router** decides which documents touch which terms (tags, enrichment, semantic score). Each gets
   a shortlist from **hybrid search** (tags + keyword + semantic, rank-fused).
6. **Doc Readers** are small agents, each bound to one document. They see a two-level outline (articles, then
   sections), choose sections with a reason, read them, and record each value with an exact quote.
7. **Reconcile:** the latest effective date wins, and the text's stated date beats the database date. A document that
   doesn't mention a term keeps the old value. These are flagged for human review, never resolved silently:
   - same-date conflicts;
   - "FY2025 only" scoped overrides;
   - missing dates;
   - unverified quotes.

## Agents and tools

| Agent | Kind | Model | Job | Main tools |
| --- | --- | --- | --- | --- |
| Digitization | LLM map-reduce | gpt-5.4-mini | Section summaries + tags, article/document summaries, effective date, scope | (structured output) |
| Coverage router | code | none | (document, term group) work items + shortlists | `hybrid_search` |
| Doc Reader | LLM agent, one per item | gpt-5.5 | Read one document, record values with quotes | `get_article_sections`, `read_chosen_sections`, `read_section`, `search_in_doc`, `record_finding` |
| Coverage check | code | none | Strong evidence never read → re-queue once | ledger, reports |
| Reconciler | code + LLM | gpt-5.5 (conflicts only) | Date rules; proposes pick / keep prior / escalate | `reconcile`, `read_section`, `verify_quote` |
| Human review | pause | person | Resolves remaining conflicts | review queue |
| Summarizer | LLM | gpt-5.5 | Narrative + publish versioned extract | `publish_extract` |
| Chat router | LLM | gpt-5.4-mini | Routes to Q&A / Term Finder / Drafting | (structured output) |
| Trader Q&A | LLM agent | gpt-5.5 | Answers about one trader, with citations | `get_trader_extract`, `get_term`, `search_sections`, `read_section` |
| Term Finder | LLM agent | gpt-5.5 | Which documents / traders have a term; wording variants | `find_docs_with_term`, `get_clause_variants` |
| Drafter | LLM agent | gpt-5.5 | Drafts a DOCX from precedent clauses, for review | `get_clause_variants`, `render_docx` |

## Guardrails

- **Every recorded value carries an exact quote,** checked against the section text (exact, then OCR-tolerant).
  Values with unverified quotes go to review.
- **`record_finding` rejects bad input:** unknown taxonomy paths, near-duplicate keys, a "removal" of a provision
  that was really replaced, and scope notes that aren't period limits.
- **Enrichment, tags and semantic hits only point to sections;** they're never evidence for a value.
- **Agents are fenced in:** each tool is bound to its trader (and Doc Readers to their one document), and
  cross-trader results hide clause text from callers who aren't entitled to it.
- **Data stays private:** embeddings run locally, and the OpenAI agent loop uses `store=False` so contract text isn't
  kept by OpenAI.

## Interfaces

| Interface | What it offers |
| --- | --- |
| Business UI (`ui/app.py`, port 8501) | Extract (term tree, versions, diff) · Review (resolve conflicts) · Chat (answers + citations, "show on page" highlight) · Term Finder · Drafts |
| Dev Console (`devconsole/app.py`, port 8502) | **Ask & trace** (each agent's steps, tools, responses, sections chosen and why) · **Document lifecycle** (10 steps from database row to ready, with stored data) · Runs · Run detail (graph, span tree, timeline) · State inspector (checkpoints, fork) · Data explorer · Tool playground · Prompts & evals |
| REST API (`compdoc.api`) | traders, ingest, extract, reviews, chat, drafts, term search |
| MCP server (`compdoc.mcp_server`) | the read tools, for Claude Desktop / Cursor |
| CLI (`compdoc`) | ingest, extract, review, show, chat, find, draft, traders |

## Data (SQLite, `var/compdoc.db`)

| Tables | Holds |
| --- | --- |
| `documents`, `conversions` | Source rows (mock of the firm's document database), Docling status and quality checks |
| `sections`, `section_text`, `section_spans` | Clause tree, normalised text, character position → Docling item, page, box |
| `section_enrichment`, `article_enrichment`, `doc_enrichment`, `section_vectors` | Cached enrichment (by section hash + prompt + model) and local embeddings |
| `findings`, `extracts`, `reviews`, `audit` | Evidence ledger, versioned term trees, review queue, audit log |
| `runs`, `spans` | Trace of every run: node → agent → LLM call / tool call, with tokens and cost |
| `entitlements`, `drafts` | Mock access control, drafted documents |

Docling JSON and markdown are cached in `var/cache/`; LangGraph checkpoints in `var/checkpoints.db`.

## Evals and benchmarks (latest)

| Command | Measures | Latest result |
| --- | --- | --- |
| `evals/run_evals.py` | Values vs gold: F1, current-value accuracy, finding recall, citations, conflicts, cost | Live (before scaling): 1.0 on the small set, ~$0.15 per trader · Offline now: small set 1.0; large set 0.66 (only reworded clauses missed; every right section was read) |
| `evals/structure_eval.py` | Section tree vs gold | Section F1 1.0, parent accuracy 1.0 on the 200-page set |
| `evals/retrieval_eval.py` | Is the right section in the top k | Top-1: hybrid 0.92 · keyword 0.85 · semantic 0.77 |
| `benchmarks/docling_bench.py` | Docling speed and memory | ~0.2 s per page (202 pages in ~40 s), 1-2 GB per process, CPU |
| `benchmarks/shard_bench.py` | Splitting PDFs across workers | Same sections as whole-file; failures isolated; slower on one machine, so off by default |

## Configuration (`COMPDOC_*` environment variables)

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROVIDER` | openai | openai / anthropic / offline |
| `AGENT_MODEL`, `FAST_MODEL` | gpt-5.5, gpt-5.4-mini | Agents vs enrichment and routing |
| `ENRICH_CONCURRENCY`, `ENRICH_BATCH_TOKENS`, `LLM_RPM`, `LLM_TPM` | 8, 8000, 300, 400000 | Enrichment parallelism and shared rate limit |
| `ROUTE_SEM_MIN` | 0.55 | Semantic score that makes a term relevant to a document |
| `EMBED`, `EMBED_MODEL` | 1, all-mpnet-base-v2 | Local embeddings on/off and model |
| `CONVERT_WORKERS`, `SHARD_PAGES`, `SHARD_MIN_PAGES` | 1, 40, 60 | Sharded conversion (set workers ≥ 2 only with several machines) |
| `OCR_ENGINE` | ocrmac | ocrmac / easyocr / tesseract |
| `VAR_DIR`, `SOURCE_DIR`, `REDACT_SPANS`, `LOG_LEVEL` | var, data/synth/out, 0, WARNING | Storage, default source, hide text in traces, logging |

## Project layout

| Path | What |
| --- | --- |
| `src/compdoc/ingest.py`, `structure.py`, `conversion.py` | Docling conversion (whole or sharded), section builder, QC, text store |
| `src/compdoc/agents/` | digitizer (enrichment), doc_reader (router, Doc Readers, coverage), reconciler, summarizer, chat, drafter, offline rules |
| `src/compdoc/tools/` | docs and outlines, BM25 search, hybrid search, ledger, verify, reconcile, extracts, clauses, citations, access, registry |
| `src/compdoc/graph.py`, `service.py`, `llm.py`, `observability.py` | Extraction graph, shared service, provider layer, tracing |
| `src/compdoc/prompts/*.md`, `taxonomy.yaml` | Prompts (versioned by content hash), term taxonomy |
| `ui/`, `devconsole/` | Business UI, Dev Console |
| `data/synth/` | Synthetic contract generators and gold labels |
| `evals/`, `benchmarks/`, `tests/` | Quality, speed, tests |
| `docs/scaling-plan.md` | Scaling plan with measured results and build status |

## Running on AWS

| Local piece | AWS equivalent |
| --- | --- |
| LLM providers | Add a Bedrock provider in `llm.py`; nothing else changes |
| SQLite database and checkpoints | Aurora Postgres + `langgraph-checkpoint-postgres` |
| Docling cache, source files | S3 |
| Embeddings and keyword search | pgvector or OpenSearch (keyword + vector), filtered by trader |
| Ingest and extraction | Airflow / Temporal jobs; conversion workers on separate machines or GPUs; human review = a signal that resumes the graph |
| Traces | OpenTelemetry to your monitoring tool; the Dev Console keeps reading the spans table |

## Next steps

1. Add OpenAI credits and run the live eval on both test sets: it checks the new LLM paths, reworded-clause reading,
   and real cost per trader.
2. Test on a few real, redacted contracts (section structure, routing threshold, semantic search value).
3. Build the remaining milestones in [docs/scaling-plan.md](docs/scaling-plan.md).
