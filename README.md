# comp-doc-extractor

A multi-agent system that reads a trader's compensation contracts and returns their **current terms as a tree**:
term → sub-term → leaf value. Every value cites the document, section, page and quote it came from, and shows its
history across documents. The same data serves a chatbot, a cross-trader term finder and a precedent-based drafting
agent. A separate **Dev Console** shows every step from the orchestrator down to the stored rows.

All contracts in `data/synth/` are synthetic.

## Quick start

```bash
uv sync                                   # Python 3.12; the first Docling run downloads its layout/table models
cp .env.example .env                      # add OPENAI_API_KEY (or set COMPDOC_PROVIDER=offline)
uv run python data/synth/generate.py      # 26 synthetic contracts for 10 traders + gold labels
uv run compdoc ingest                     # Docling → sections → LLM enrichment (cached by file hash / prompt version)
uv run compdoc extract T-1003             # multi-agent extraction → term tree
uv run compdoc extract T-1004             # same-date conflict → pauses for review
uv run compdoc review <run_id> --pick 'Compensation.Payout.payout_pct_net_pnl=<finding_id>' --reviewer me
uv run compdoc chat "When did T-1003's deferral change?"
uv run compdoc find Clawback
uv run compdoc draft T-1003 "Draft an amendment raising payout to 18% and moving to 4 year vesting"

uv run streamlit run ui/app.py --server.port 8501          # business UI: Extract | Review | Chat | Term Finder | Drafts
uv run streamlit run devconsole/app.py --server.port 8502  # developer UI
uv run uvicorn compdoc.api:app --port 8000                 # REST API (X-Caller header = identity)
uv run python -m compdoc.mcp_server                        # MCP server (stdio) for Claude Desktop / Cursor
uv run pytest                                              # offline tests (no API key needed)
uv run python evals/run_evals.py                           # scorecard vs gold labels
```

`COMPDOC_PROVIDER` can be `openai` (default: `gpt-5.5` for agents, `gpt-5.4-mini` for enrichment), `anthropic`, or `offline`.
In offline mode every agent runs a rule-based policy that calls **the same tools**, so traces, tests and the UIs all
work without an API key.

## How it works

```
                       INGEST (once per document, cached)
 source DB row ─► Docling convert ─► section builder ─► QC ─► Digitization Agent (enrich)
 (trader_id, doc_date, path)  (layout, TableFormer, OCR)  (clause numbering, heading repair)   (summary, term tags, definitions,
                                                                                               cross refs, scope, effective date)
                       EXTRACTION GRAPH (LangGraph, checkpointed)
 trader_id ─► orchestrator ─► coverage router (code: tags + keyword + semantic) ─► Send × N ─►
              Doc Reader (one per document; term groups for long documents; bounded context)
                 tools: get_article_sections, read_chosen_sections, read_section, search_in_doc, record_finding
              ─► coverage check (re-queue gaps once) ─► evidence ledger
            ─► reconciler (deterministic `reconcile` + LLM only on conflicts)
            ─► human_review (interrupt) if conflicts remain
            ─► summarizer + publish (versioned extract)

                       CHAT  (router → trader_qa | term_finder | drafting)
```

**Reconcile rules.** The latest *effective* date wins; the date stated in the document text overrides the DB date.
A later document that doesn't mention a term leaves the earlier value in place. "Deleted in its entirety" removes a
term. The following are flagged for review, never resolved silently:
- same-date different values
- scoped overrides ("Performance Year 2025 only")
- missing dates
- unverified quotes

**Guardrails.**
- Every finding's quote is checked against the cited section's Docling text (`verify_quote`).
- Enrichment output is used only for navigation, never as evidence.
- `record_finding` rejects removals of "replaced" provisions and drops scope notes that aren't period limits.
- Tags must exist in the taxonomy.
- Tools are bound to the trader and caller, so an agent can't reach another trader's documents.
- Cross-trader tools withhold clause text from callers who aren't entitled to it.
- The OpenAI agent loop uses the Responses API with `store=False`, so OpenAI keeps no copy of contract text.

## Scale

See [docs/scaling-plan.md](docs/scaling-plan.md) for the plan and build status: real-contract section builder
(Articles, `Section 4.02`, `(a)(i)`, schedules), section text store for quote checks, enrichment map-reduce with a
per-section cache, Doc Readers with two-level outlines and hybrid search (local embeddings), and an optional sharded
Docling path. Large synthetic contracts: `uv run python data/synth/generate_large.py` then
`uv run compdoc ingest --source data/synth/out_large`. Evals: `evals/structure_eval.py`, `evals/retrieval_eval.py`,
`evals/run_evals.py`; benchmarks in `benchmarks/`.

## Layout

| Path | What |
|---|---|
| `src/compdoc/ingest.py` | Docling conversion, section builder (numbering + heading repair), QC, cache |
| `src/compdoc/agents/` | digitizer, researcher, reconciler, summarizer, chat (router / Q&A / finder), drafter, offline rules |
| `src/compdoc/tools/` | docs, search (BM25), ledger, verify, reconcile, extracts, clauses, access, registry |
| `src/compdoc/graph.py` | LangGraph graph + SQLite checkpointer |
| `src/compdoc/llm.py` | provider layer: structured output + traced tool-calling loop (OpenAI Responses / Anthropic / offline) |
| `src/compdoc/observability.py` | runs → spans tree (node / agent / llm / tool) stored in SQLite |
| `src/compdoc/prompts/*.md` | prompts; the version is a content hash recorded on every span |
| `ui/app.py`, `devconsole/app.py` | business UI, developer UI |
| `evals/run_evals.py` | scorecard: leaf P/R/F1, current-value accuracy, finding recall, citation validity, conflict P/R, tag recall, cost |

## Running on AWS

| Local piece | AWS equivalent |
|---|---|
| `llm.py` providers | Add a Bedrock provider (`AnthropicBedrockMantle` / Converse); nothing else changes |
| `var/compdoc.db` | Aurora Postgres (tables are plain SQL) |
| Docling cache | S3 |
| Checkpointer | `langgraph-checkpoint-postgres` |
| Ingest | An Airflow/Temporal batch job per new document event |
| Extraction | A Temporal workflow per trader; human review = a signal that resumes the graph |
| Traces | Exported via OpenTelemetry to your APM; the Dev Console keeps working on the spans table |
