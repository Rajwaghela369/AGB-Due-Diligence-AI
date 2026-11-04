# AGB-Check: Agentic AGB Review for German Legal Due Diligence

Nov 2, 2026 · @Raj

## Project brief

AGB-Check is an agentic RAG assistant that flags potentially void clauses in German consumer terms and conditions (AGB), cites the BGB paragraph, and explains why. It is a learning prototype in the Legal Due Diligence area, built with Python, LangGraph, Langfuse and a React frontend over the AG-UI protocol.

**Problem.** Invalid AGB clauses expose a company to Abmahnungen and leave it unprotected in disputes. Lawyers check AGB clause by clause against §§ 305–310 BGB, which is slow and repetitive.

**Users.**

- Primary: a lawyer or legal associate reviewing a company's AGB, for a client or in a due diligence
- Secondary: an in-house legal team checking its own shop terms before launch

**Scope of the prototype.**

- In: German B2C online-shop AGB; liability (Haftung) and warranty (Gewährleistung) clauses; German law only
- Out: other clause topics, B2B contracts, foreign law, final legal advice (the lawyer decides)

**Goals.**

1. Find void clauses reliably: recall on void clauses is the main metric, since missing one is worse than a false alarm
2. Ground every verdict in a retrieved BGB paragraph, never in the model's memory alone
3. Let the lawyer see each step and overrule uncertain verdicts
4. Beat the published AGB-DE baselines (best F1 0.35 on the full corpus, 0.54 on the undersampled version)

## Real-world scenario

A German online shop is being acquired, and the buyer's law firm must report whether the shop's AGB create legal risk. AGB-Check turns hours of clause-by-clause reading into a reviewed list of flagged clauses.

**Why it matters in Germany.**

- Standard terms are controlled by §§ 305–310 BGB. A void clause is replaced by the statutory rule, so the company loses the protection it wrote in.
- Competitors and consumer associations (Verbraucherzentralen) can send an Abmahnung for invalid clauses, with a cease-and-desist demand and costs.
- Liability exclusions are a frequent problem: § 309 Nr. 7 BGB forbids excluding liability for injury to life, body or health, and for gross negligence.

**Today.** An associate reads every clause, compares it to the BGB and case law, and fills a risk sheet. It is slow, and tired readers miss clauses.

**With AGB-Check.**

1. The associate uploads the AGB.
2. The agent checks each liability and warranty clause, retrieves the matching BGB paragraphs, and gives a verdict with reasons.
3. Uncertain verdicts are sent to the associate for a decision.
4. The result feeds the DD report, for example: "4 clauses likely void under § 309 Nr. 7 BGB; revise the AGB before closing."

In due diligence terms, this covers **Legal DD → customer contracts → AGB compliance**.

## AI system design

A LangGraph agent checks each clause in five steps and streams every step to the React UI over AG-UI, while Langfuse records the whole run.

&#91;embedded content: AGB-Check architecture · frontend, agent, services\]

A verdict that cites no retrieved paragraph goes back to retrieval; only uncertain verdicts reach the lawyer.

**Agent steps**

1. **Topic router:** confirms the clause is liability or warranty and picks the matching BGB area.
2. **Retrieve BGB:** pulls the most relevant paragraphs (e.g. § 309 Nr. 7, § 307) from the vector store.
3. **Judge:** decides valid, potentially void or uncertain, with a short German reason and confidence.
4. **Self-check:** verifies the reason refers to a retrieved paragraph; if not, it retries retrieval once.
5. **Lawyer review:** pauses the run for uncertain clauses and waits for the lawyer's decision in the UI.

**Why it is agentic:** the agent chooses the legal area, decides when evidence is insufficient, loops back, and hands off to a human, instead of one fixed prompt.

**Tech stack**

| Layer | Choice |
| --- | --- |
| Frontend | React + CopilotKit components |
| Agent-UI protocol | AG-UI (streamed events, shared state, human-in-the-loop) |
| Backend | Python, FastAPI |
| Agent framework | LangGraph |
| Retrieval | Chroma or Qdrant, German-capable embedding model |
| LLM | EU-hosted API or local model |
| Observability and eval | Langfuse (self-hosted or EU region) |
| Storage | SQLite |

## Input and output

The user gives one set of AGB; the system returns a clause-level risk table plus a short summary, streamed live.

**Inputs**

| Input | Format | Notes |
| --- | --- | --- |
| AGB document | Pasted text, .txt or PDF | Prototype: clauses from AGB-DE, already split |
| Review scope | Checkbox | Liability and warranty in v1 |
| Lawyer decisions | Button click in UI | Only for clauses the agent marks uncertain |
| Follow-up questions | Chat text | e.g. "Rewrite clause 6.1 to comply" |

**Outputs**

| Output | Content |
| --- | --- |
| Clause risk table | Clause, topic, verdict (valid / potentially void / uncertain), BGB paragraph, reason in German, confidence |
| Live step feed | Current agent step and retrieved paragraphs, via AG-UI events |
| Review cards | Uncertain clauses waiting for the lawyer |
| Summary | Count of void clauses by paragraph, top risks, recommendation |
| Export | Table as CSV or Markdown for the DD report |

**Example result for one clause**

- **Clause:** 6.1 "Unsere Haftung ist ausgeschlossen und auf das gesetzlich zulässige Maß beschränkt."
- **Topic:** Liability
- **Verdict:** Potentially void
- **Legal basis:** § 309 Nr. 7 BGB; § 307 Abs. 1 S. 2 BGB (transparency)
- **Reason:** Blanket exclusion also covers injury to life and health and gross negligence, which cannot be excluded in consumer AGB. The wording is also contradictory.
- **Confidence:** High

This clause is a real example from AGB-DE, labeled void by the experts.

## Data design

Two German sources feed the system: AGB-DE clauses as the input and test set, and BGB paragraphs as the knowledge base the agent retrieves from.

**Sources**

| Source | Content | Size used in v1 | Role | License |
| --- | --- | --- | --- | --- |
| [AGB-DE](https://github.com/DaBr01/AGB-DE) (TU Munich) | 3,764 expert-labeled clauses from 93 German shop AGB | Liability: 211 clauses (19 void); warranty: 314 (20 void) | Input clauses + ground truth | CC BY-SA 4.0 |
| BGB via [Open Legal Data](https://openlegaldata.io/research/2026/05/22/may-2026-dataset-available.html) | German federal laws | §§ 305–310, warranty rules §§ 434–445, 474–479 | Knowledge base for retrieval | No copyright (§ 5 UrhG) |

Void clauses are rare (4.8% overall), so accuracy is a misleading metric; precision and recall on the void class are used instead.

**Data layers**

1. **Raw:** AGB-DE CSV and BGB text, stored unchanged
2. **Processed:** clauses filtered to liability and warranty, foreign-law references removed, test split fixed
3. **Index:** BGB paragraphs chunked one per paragraph or numbered item (e.g. § 309 Nr. 7 as its own chunk), embedded into a vector store with metadata
4. **Results:** one verdict record per clause per run
5. **Evaluation:** the labeled test split, uploaded as a Langfuse dataset

**Core records**

```json
{
  "clause": {
    "clause_id": "agb-1-6.1",
    "contract_id": 1,
    "title": "6. Haftung",
    "text": "Unsere Haftung ist ausgeschlossen ...",
    "topics": ["liability"],
    "label_void": 1
  },
  "law_chunk": {
    "chunk_id": "bgb-309-7",
    "law": "BGB",
    "paragraph": "§ 309 Nr. 7",
    "title": "Haftungsausschluss bei Verletzung von Leben, Körper, Gesundheit und bei grobem Verschulden",
    "text": "...",
    "topics": ["liability"]
  },
  "verdict": {
    "run_id": "2026-11-02-v1",
    "clause_id": "agb-1-6.1",
    "verdict": "potentially_void",
    "legal_basis": ["bgb-309-7", "bgb-307-1"],
    "reason_de": "...",
    "confidence": 0.9,
    "lawyer_decision": null,
    "langfuse_trace_id": "..."
  }
}
```

**Storage**

| Data | Store |
| --- | --- |
| Clauses, verdicts, lawyer decisions | SQLite (Postgres later) |
| BGB chunks + embeddings | Chroma or Qdrant |
| Traces, scores, eval datasets, prompts | Langfuse |

## Evaluation, observability and privacy

Langfuse measures whether the agent finds void clauses and shows why it decided as it did; privacy rules keep the system itself DSGVO-compliant.

**Metrics**

| Metric | What it shows | Target for v1 |
| --- | --- | --- |
| Recall (void class) | Share of void clauses found | Main metric; aim above 0.70 |
| Precision (void class) | Share of flags that are truly void | Keep false alarms manageable |
| F1 (void class) | Balance of both | Above the 0.35 baseline |
| Grounding rate | Verdicts citing a retrieved paragraph | 100% |
| Retrieval hit | Correct paragraph in top-k results | Checked on a hand-labeled sample |
| Cost and latency per clause | Tokens and seconds | Tracked per run |
| Lawyer agreement | Overrides vs agent verdicts | Logged from the UI |

**How Langfuse is used**

- **Traces:** one trace per clause, with a span per agent node and per retrieval
- **Datasets and experiments:** the labeled test split runs as an experiment; prompt, model and chunking variants are compared side by side
- **Scores:** automatic scores against the AGB-DE label, LLM-as-judge for reason quality, and lawyer decisions as human feedback
- **Prompt management:** prompts versioned in Langfuse so each result is tied to the prompt that produced it

**Privacy by design (DSGVO)**

- AGB-DE is already anonymized (names replaced by placeholders); real client AGB in later phases may contain names and addresses
- Personal data is masked before it is sent to the LLM and before traces are stored (Langfuse masking)
- Langfuse is self-hosted or used in its EU region; the LLM is EU-hosted or local
- The LLM provider is a processor under Art. 28 DSGVO and needs an AVV, the same check planned for phase 2

## Roadmap

Build the agent and prove its accuracy first, then add the interface; each stage ends with a Langfuse result to compare against.

1. **Data and baseline:** load AGB-DE, filter liability and warranty, build the BGB index, run a plain one-shot LLM classifier as the baseline. Gate: baseline F1 recorded in Langfuse.
2. **Agentic RAG:** LangGraph agent with topic routing, BGB retrieval, judgment and self-check. Gate: recall and F1 above the baseline.
3. **AG-UI streaming:** FastAPI backend exposing the agent over AG-UI; minimal React page with step feed and live risk table.
4. **Human in the loop:** review cards for uncertain clauses; lawyer decisions logged as Langfuse scores.
5. **Polish:** clause highlighting, summary, export, rewrite suggestions.

**Phase 2: DSGVO extension.** Add data processing agreements (AVV) checked against Art. 28(3) DSGVO, with a small hand-labeled test set, staying in the German legal DD direction.
