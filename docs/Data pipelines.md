# Data pipelines

Nov 2, 2026

Two independent pipelines: **P1 builds the BGB knowledge base** (vector database) and runs first; **P2 prepares AGB-DE as test data** (input clauses plus answer key). P2 never writes into the knowledge base.

## Overview

&#91;embedded content: P1 and P2 · steps, artifacts, outputs\]

The vector index is the knowledge base; the clause table and the Langfuse dataset hold the same clauses, but only the dataset carries the answer key.

## P1 · KB-INGEST: knowledge base ingestion

Turns about 40 BGB paragraphs into item-level chunks in a vector database; build and validate this first.

| Step | Script | Input | Output artifact |
| --- | --- | --- | --- |
| 1.1 Acquire | `p1_kb/s1_fetch_laws.py` | `openlegaldata/laws-germany`, config `dump-20260520` | `data/raw/laws/laws_dump-20260520.parquet` |
| 1.2 Select | `p1_kb/s2_select_norms.py` | raw dump + `configs/kb_paragraphs.yaml` | `data/interim/bgb_selected.jsonl` |
| 1.3 Chunk | `p1_kb/s3_chunk_norms.py` | selected paragraphs | `data/processed/bgb_chunks.jsonl` |
| 1.4 Embed and index | `p1_kb/s4_build_index.py` | chunks + `configs/kb.yaml` | Chroma collection `bgb_agb_v1` in `stores/chroma/` |
| 1.5 Validate | `p1_kb/s5_check_retrieval.py` | index + `data/gold/kb_queries.jsonl` | `reports/p1_retrieval_check.json` |

**Chunk record** (`bgb_chunks.jsonl`, one line per item)

```json
{"chunk_id": "bgb-309-7", "law": "BGB", "paragraph": "§ 309", "item": "7", "sub_item": null,
 "title": "Klauselverbote ohne Wertungsmöglichkeit", "topics": ["liability"],
 "embed_text": "BGB § 309 Klauselverbote ohne Wertungsmöglichkeit, Nr. 7: ...",
 "text": "...", "source_dump": "dump-20260520", "source_url": "https://www.gesetze-im-internet.de/bgb/__309.html"}
```

**Engineering notes**

- Chunk IDs are deterministic (`law-paragraph-item-subitem`), so reruns overwrite instead of duplicating
- `embed_text` carries the law, paragraph and title prefix; `text` stays clean for display and citation
- Embedding model must handle German, e.g. `BAAI/bge-m3` or `intfloat/multilingual-e5-large`; the name and version go in `configs/kb.yaml`
- **Quality gate:** about 20 hand-written queries with the expected chunk (e.g. a blanket liability exclusion → `bgb-309-7`); starting target: expected chunk in the top 5 for at least 90% of queries

## P2 · EVAL-PREP: evaluation data preparation

Turns AGB-DE into in-scope input clauses for the agent and a separate answer key for Langfuse; 523 liability and warranty clauses, 39 of them void.

| Step | Script | Input | Output artifact |
| --- | --- | --- | --- |
| 2.1 Acquire | `p2_eval/s1_fetch_agb.py` | GitHub `DaBr01/AGB-DE` | `data/raw/agb-de/agb-de-anonym.csv` |
| 2.2 Normalize | `p2_eval/s2_normalize.py` | raw CSV | `data/interim/agb_clauses.parquet` |
| 2.3 Scope filter | `p2_eval/s3_filter_scope.py` | normalized clauses | `data/processed/agb_scope_v1.parquet` |
| 2.4 Split | `p2_eval/s4_split.py` | scoped clauses + authors' split IDs | `data/processed/splits/train.parquet`, `test.parquet` |
| 2.5 Publish | `p2_eval/s5_publish.py` | splits | table `clauses` in Neon Postgres; Langfuse dataset `agb-de-lw-test-v1` |

**What each step does**

- **Normalize:** topics to lists, `void` to boolean `label_void`, title split into `section_no` and `section_title`, `is_fragment` flag for texts under 40 characters, cited § references extracted
- **Scope filter:** keep liability or warranty; drop fragments and shops referring to foreign law
- **Split:** reuse the authors' train/test assignment so results stay comparable to the paper
- **Publish:** the clause table holds no labels; labels go only into the Langfuse dataset as expected output

**Engineering notes**

- Keep `<<NAME>>`, `<<STADT>>`, `<<STRASSE>>` placeholders unchanged
- **Quality gate:** row counts and void counts per split logged to `reports/p2_counts.json`; a check fails the run if any label appears in the clause table

## Repository layout

One folder per pipeline, numbered scripts, and data stored by stage (`raw` → `interim` → `processed`).

```text
agb-check/
├── configs/
│   ├── kb.yaml                 # embedding model, collection name, chunk rules
│   └── kb_paragraphs.yaml      # BGB paragraph whitelist
├── p1_kb/                      # P1 · KB-INGEST
│   ├── s1_fetch_laws.py
│   ├── s2_select_norms.py
│   ├── s3_chunk_norms.py
│   ├── s4_build_index.py
│   └── s5_check_retrieval.py
├── p2_eval/                    # P2 · EVAL-PREP
│   ├── s1_fetch_agb.py
│   ├── s2_normalize.py
│   ├── s3_filter_scope.py
│   ├── s4_split.py
│   └── s5_publish.py
├── data/
│   ├── raw/        laws/, agb-de/          # never edited
│   ├── interim/                            # cleaned, not final
│   ├── processed/                          # pipeline outputs
│   └── gold/       kb_queries.jsonl        # hand-written retrieval checks
├── stores/         index manifest; vectors in Qdrant Cloud, tables in Neon Postgres
├── reports/        p1_retrieval_check.json, p2_counts.json
└── Makefile        # make kb, make eval-data
```

Run order: `make kb` (P1) first, then `make eval-data` (P2).

## Source reference

| Source | Role | Raw format | Key fields | License |
| --- | --- | --- | --- | --- |
| BGB via [Open Legal Data](https://openlegaldata.io/research/2026/05/22/may-2026-dataset-available.html) | Knowledge base (P1) | Hugging Face dataset; one record per whole paragraph; HTML and Markdown text | `book_code`, `section`, `title`, `markdown_content`, `slug`, `order` | No copyright (§ 5 UrhG) |
| [AGB-DE](https://github.com/DaBr01/AGB-DE) | Input clauses + answer key (P2) | One CSV, 3,764 clause rows, 8 columns | `id`, `contract`, `title`, `text`, `topics`, `subtopics`, `void` | CC BY-SA 4.0 |

BGB field names come from the provider's documentation; confirm them on first download.
