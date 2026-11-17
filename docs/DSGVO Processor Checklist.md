# DSGVO Processor Checklist (Phase 9, 9.F)

> **Not legal advice.** This is an engineering research document, prepared 2026-11-17 from
> the vendor and primary-law pages linked inline. It has not been reviewed by a lawyer or a
> data protection officer. Vendor terms change; every row must be re-checked against the live
> page and the **signed** contract before the app handles a real client contract. Per the
> Phase 7-10 plan (9.F), nothing below is "confirmed" until a link to the signed AVV is added.
> Where a fact could not be verified, it says **UNVERIFIED**; none of those were guessed.

Status of the app when this was written: `privacy.masking.enabled: false`,
`retention.enabled: false`, `auth.enabled: false`, `files.backend: local`
(`configs/app.yaml`). The measures in section 4 exist as code but are **switched off in the
shipped config**; they must be switched on (and `masking.required: true`) before real client
data is processed.

## 1. Processor table

"Receives" describes the data **after Phase 9 masking is on** (see
`docs/Phase 9 Design.md`, "Where personal data lives today"). Masking covers PERSON,
ADDRESS, EMAIL, PHONE, IBAN and REGISTER_NUMBER; NER misses are possible, so residual
personal data in the clause text must be assumed.

| # | Processor (role in app) | Receives (masked) | Location / region options | DPA / AVV | Transfer mechanism | Sub-processors | Action needed |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | **Google, Gemini API** (judge, explain, summary, chat) | Masked clause text, statute passages, chat history | Terms say prompt/response data "may be stored transiently or cached in any country in which Google or its agents maintain facilities" ([terms](https://aistudio.google.com/docs/terms)). **No EU-only guarantee for the Gemini API found.** Vertex AI regional endpoints are a different product; EU residency there is only from secondary sources, **UNVERIFIED** against Google Cloud docs. | Paid use is processed under the "Data Processing Addendum for Products Where Google is a Data Processor" ([terms](https://aistudio.google.com/docs/terms), [processor terms](https://business.safety.google/processorterms/)). Accepted by agreeing to the terms (click-through). Paid status requires a Cloud project with active billing; for EEA/CH/UK users the paid-data terms are stated to apply also to unpaid use ([terms](https://aistudio.google.com/docs/terms)). Paid: prompts not used to improve products; logged for a limited period for abuse detection (same page). | Google applies a "Data Transfer Solution" such as the EU-US DPF, else SCCs ([processor terms](https://business.safety.google/processorterms/)). | List at [business.safety.google/subprocessors](https://business.safety.google/subprocessors/); 30 days' email notice, objection by terminating within 90 days ([processor terms](https://business.safety.google/processorterms/)). | (a) Confirm the project is on a billed (Paid) tier; never use the free tier for client text. (b) Record the DPA acceptance date. (c) **Decision:** accept non-EU processing under SCCs/DPF, or evaluate Vertex AI with an EU region (verify against Google Cloud docs). (d) Keep masking on. |
| 2 | **Jina AI, embeddings** (router topic embedding) | Masked clause text (query) | Jina AI GmbH is Berlin-based, but "now part of Elastic"; processing location of API data **not stated** on [jina.ai/legal](https://jina.ai/legal) (the site itself is hosted in the USA). EU-only option: **UNVERIFIED**. | Page says the applicable DPA is the "Elastic Customer DPA" (a second passage names the "Elastic Data Processing Addendum"); it warns terms "may no longer reflect the current data processing practices" ([jina.ai/legal](https://jina.ai/legal)). Signing route: **UNVERIFIED**, ask Elastic. | **UNVERIFIED** (no mechanism named on the page). | Jina may involve subcontractors; no list found for API data ([jina.ai/legal](https://jina.ai/legal)). | Obtain the current Elastic DPA with SCC/DPF terms and sub-processor list in writing; confirm region. Same page states inputs are stored "only ... to the extent required to provide the Services" and not used for training (s. 4.6, 10.4). If no EU commitment is available, consider a self-hosted embedding model (the router only needs topics). |
| 3 | **Qdrant Cloud** (vector store, alias `bgb_agb`) | Statute/AGB-DE vectors (public text), plus query vectors derived from masked clauses. Whether any client text is stored as payload: **not checked in code**, confirm. | Primarily EEA, "mainly Germany and Ireland", limited US transfers under DPF/SCCs ([DPA Annex 1](https://cloud.qdrant.io/dpa)). Cluster provider choice AWS/GCP/Azure; a specific EU region list was not on the docs page ([docs](https://qdrant.tech/documentation/cloud/create-cluster/)), pick EU region in the dashboard and screenshot it. | Yes. [cloud.qdrant.io/dpa](https://cloud.qdrant.io/dpa), Qdrant Solutions GmbH, Berlin. Breach notice within 48 h; deletion/return incl. backups; yearly audit rights (same page). | EU SCCs Modules 2 and 3, UK addendum, Swiss adaptation (s. 11, same page). | List at [qdrant.to/trust-center](https://qdrant.to/trust-center); general authorisation, 2 weeks to object (DPA s. 8). | Create the cluster in an EU region; confirm which collections hold client-derived vectors (if only public norms, the DPA is low-risk). Record DPA acceptance in the service agreement. |
| 4 | **Neon (Databricks), Postgres** (documents, clause texts, verdicts, decisions, outbox) | **Unmasked** original data: filenames, clause texts, notes, generated reasoning. This is the main store of personal data. | AWS Frankfurt `aws-eu-central-1` and London available ([regions](https://neon.com/docs/introduction/regions)); region is fixed per project (same page). Azure regions deprecated for new projects (same page). Point-in-time-restore window: **UNVERIFIED** (not on that page). | Neon is now under the Databricks Master Cloud Services Agreement with a Neon Product Specific Schedule that refers to a DPA ([schedule](https://neon.com/dpa)). Neon's older standalone DPA PDF exists ([neon.tech/pdf/DPA.pdf](https://neon.tech/pdf/DPA.pdf)), whether it is still current: **UNVERIFIED**. Databricks DPA: [databricks.com/dpa](https://databricks.com/dpa) (v3, 2023-07-21, per search result). | Databricks publishes a [Data Transfer Addendum](https://www.databricks.com/legal/data-transfer-addendum) incorporating SCCs (search result, not read in full). | Neon's schedule names Grafana Labs (US) for infrastructure; Databricks list: [databricks-subprocessors](https://www.databricks.com/legal/databricks-subprocessors) (Neon itself is not listed there; AWS "customer selected"). | (a) Project in `aws-eu-central-1`; screenshot it. (b) Find the DPA that is actually in force for the account (Databricks vs Neon PDF) and file it. (c) Obtain the PITR/backup retention window and write it into the retention policy (design doc promises this in 9.F). (d) Ask whether Grafana Labs (US) sees customer data. |
| 5 | **Langfuse Cloud** (traces, scores, datasets) | Masked prompts/outputs via `Langfuse(mask=...)`; masked score comments and dataset items. If masking misses, clause text lands here (30-day trace retention planned). | EU (AWS Ireland, `cloud.langfuse.com`), US, Japan, HIPAA regions; data and accounts isolated per region; backups stay in the same jurisdiction ([data regions](https://langfuse.com/security/data-regions), per search summary). Self-hosting is the alternative (not verified here). | Yes: Langfuse DPA now hosted by ClickHouse ([DPA](https://clickhouse.com/legal/agreements/data-processing-addendum), `langfuse.com/security/dpa` redirects there). Forms part of the Agreement, no separate signature; SCCs deemed executed with it (s. 6.2, 6.4); audit reports on request; breach notice "promptly" with **no fixed deadline** (s. 7). Langfuse was acquired by ClickHouse: confirm the contracting entity (Langfuse GmbH vs ClickHouse). | EU SCCs Module 2/3, DPF-certified transfers excluded from restricted transfers (s. 6.2, 6.5). | [langfuse-subprocessors](https://clickhouse.com/legal/agreements/langfuse-subprocessors) (effective 2026-08-10): AWS and AWS Bedrock ("Customer Selected"); support tools in the US (Salesforce, Pylon, Google Workspace, Waypoint, GitHub, kapa.ai); ClickHouse affiliates incl. US, India, Singapore, Japan, Australia, UK; objection within 10 days of notice (DPA s. 4.3). | Use the **EU region**; confirm the project is in it. Ask Langfuse/ClickHouse whether non-EU support staff or affiliates can access EU-region customer content (list shows US/Asia affiliates). Consider self-hosted Langfuse in the EU if the answer is unsatisfactory. Keep `traces_days: 30` and test erasure of traces. |
| 6 | **Gotenberg** (DOCX/PDF conversion) | Whole exported memo (contains clause text, notes) | Open-source service run by us, so no external processor **if self-hosted in our EU infrastructure**. This could not be verified from Gotenberg docs (the fetch returned nothing); confirm from the deployment. | Not needed if self-hosted (no third party). | n/a | n/a | Confirm in `docker-compose` / prod deployment that Gotenberg is our own container and makes no outbound calls. If a hosted instance is used, it becomes a processor. |
| 7 | **S3-compatible object storage** (source PDFs; `files.backend: s3`) | Original uploaded PDFs, unmasked, server-side encrypted (`AES256`) | **Hetzner** (FSN1 Falkenstein, NBG1 Nuremberg, HEL1 Helsinki; [overview](https://www.hetzner.com/object-storage/), search summary), **Scaleway** (Paris, Amsterdam, Warsaw; [signup page](https://www.scaleway.com/en/object-storage-signup/)), **OVH**: not verified. Provider **not yet chosen**. | Hetzner: DPA concluded in the customer account, template at `hetzner.com/AV/DPA_en.pdf`, TOMs at `hetzner.com/AV/TOM.pdf`; Hetzner states the customer is responsible for the stored data and its encryption ([data protection FAQ](https://docs.hetzner.com/general/general-terms-and-conditions/data-privacy-faq)). PDFs themselves not fetched. Scaleway: DPA is part of the contract ([sub-processor list page](https://www.scaleway.com/en/subprocessorlist/)); DPA text only seen on a third-party host, verify on scaleway.com. OVH: **UNVERIFIED**. | EU provider in EU region: no third-country transfer. | Hetzner: `hetzner.com/AV/subcontractors.pdf` (third-party summary, **not read**). Scaleway: [subprocessorlist](https://www.scaleway.com/en/subprocessorlist/). | Decide the provider (open item 4). Sign the DPA in the account, set the region, and keep the endpoint/region in `configs/app.yaml` `files.s3`. Consider client-side encryption if the firm wants to be independent of the provider's keys (Hetzner places encryption responsibility on the customer). |
| 8 | **Identity provider (OIDC)** (9.E) | Name, `sub`, roles of lawyers (not client data) | Not chosen (Keycloak for dev; production "configured, not coded"). | **UNVERIFIED / n/a** until chosen. If self-hosted Keycloak in the EU, no third-party processor. | Depends on choice. | Depends on choice. | Add a row once the production provider is chosen. |

Not on the list because they receive no personal data from the app: GitHub (code only);
Hugging Face / spaCy model download (a model file is fetched, nothing is sent). Confirm
that the spaCy model `de_core_news_lg` is baked into the image so the API does not fetch it
at runtime.

### Transfer-law background (with sources)

- Art. 44-49 DSGVO: [EUR-Lex GDPR](https://eur-lex.europa.eu/eli/reg/2016/679/oj) (Chapter V). A transfer to a third country needs an adequacy decision (Art. 45), appropriate safeguards such as SCCs (Art. 46) or a derogation (Art. 49).
- EU-US Data Privacy Framework: the General Court dismissed the challenge in *Latombe v Commission* (T-553/23) on 3 September 2025; the appeal is pending as C-703/25 P ([WilmerHale](https://www.wilmerhale.com/en/insights/blogs/wilmerhale-privacy-and-cybersecurity-law/20251201-european-court-of-justice-to-review-challenge-to-eu-us-data-privacy-framework), [Baker McKenzie](https://connectontech.bakermckenzie.com/eu-us-data-privacy-framework-still-business-as-usual/), via search; I could not confirm the current procedural stage). Practical consequence: rely on SCCs as the fallback in each contract, not on DPF alone.
- SCCs need a transfer impact assessment and supplementary measures where third-country law (e.g. US surveillance law) can undermine them: [EDPB Recommendations 01/2020](https://www.edpb.europa.eu/our-work-tools/our-documents/recommendations/recommendations-012020-measures-supplement-transfer_en) (URL from the EDPB document register pattern; content not re-read here). Masking before transfer is the app's main supplementary measure. EDPB [Guidelines 01/2025 on pseudonymisation](https://www.edpb.europa.eu/our-work-tools/our-documents/guidelines/guidelines-012025-pseudonymisation_en) say pseudonymisation helps as a transfer safeguard only if the importer cannot obtain the additional information; the app keeps its placeholder map in memory only, which fits (design doc, "Reversible, but only for display"). Per search summaries; check the final version.
- Controller/processor roles and what an Art. 28 contract must say: [EDPB Guidelines 07/2020](https://www.edpb.europa.eu/our-work-tools/our-documents/guidelines/guidelines-072020-concepts-controller-and-processor-gdpr_en).
- CLOUD Act risk with US-owned providers regardless of region: reasoning recorded in `docs/Phase 9 Design.md` (Storage section); no primary source gathered here, treat as an open legal question.

## 2. Art. 28(3) DSGVO required contract points

Source: [Art. 28 GDPR](https://eur-lex.europa.eu/eli/reg/2016/679/oj). Check each processor's signed contract against every line; the table records what was visible in the public DPAs read for this document, not a full legal review.

| Art. 28 point | Requirement | Gemini (Google) | Qdrant | Langfuse | Neon/Databricks | Jina/Elastic | S3 provider |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 28(3) intro | Binding contract: subject matter, duration, nature, purpose, data types, data subject categories | Appendix in [processor terms](https://business.safety.google/processorterms/); detail not fully read | Annex 1 states duration, categories ([DPA](https://cloud.qdrant.io/dpa)) | In DPA ([DPA](https://clickhouse.com/legal/agreements/data-processing-addendum)); schedule not read | **UNVERIFIED** | **UNVERIFIED** | Hetzner: **UNVERIFIED** (PDF not read) |
| (a) | Process only on documented instructions, incl. transfers | Not read in detail | Not read in detail | Not read in detail | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| (b) | Persons authorised to process are bound to confidentiality | Not read in detail | Not read in detail | Not read in detail | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| (c) | Art. 32 security measures | ISO 27001 + Security Documentation ([terms](https://business.safety.google/processorterms/)) | TOMs in Annex 3, SOC 2 Type II aligned ([DPA](https://cloud.qdrant.io/dpa)) | Third-party audit reports on request (DPA s. 5.3) | SOC 2 report on request ([schedule](https://neon.com/dpa)) | UNVERIFIED | Hetzner TOM PDF (not read) |
| (d) | Sub-processors: prior authorisation, same obligations flow down, processor liable | Specific + general authorisation, 30 days' notice, full liability (same terms) | General authorisation, 2 weeks to object (s. 8) | 10 days to object (s. 4.3); note the shorter window | Databricks list (page read); notice terms not read | UNVERIFIED | Hetzner/Scaleway lists exist; terms not read |
| (e) | Assist with data subject rights (Art. 12-23) | Deletion assistance noted (same terms) | Not read in detail | Not read in detail | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| (f) | Assist with Art. 32-36 (security, breach, DPIA, consultation) | Breach "promptly", no fixed hours | Breach within 48 h (s. 6) | Breach "promptly", no fixed hours; DPIA support (s. 5.4) | Per Agreement (no fixed time seen) | UNVERIFIED | UNVERIFIED |
| (g) | Delete or return all data at the end, delete copies | Deletion at expiry on instruction | Return/delete incl. backups, proof on request (s. 12) | Prompt deletion after termination (s. 3.6) | UNVERIFIED (not mentioned in schedule) | UNVERIFIED | UNVERIFIED |
| (h) | Provide information to prove compliance; allow audits | Audit rights with scope agreed in advance | Yearly verification, third-party on-site | Reports on request; on-site once a year, 3 weeks' notice, customer pays (s. 5.3) | Audit "where required by applicable law" (schedule) | UNVERIFIED | UNVERIFIED |
| 28(3) last para | Processor tells controller if an instruction infringes law | Not read | Not read | Not read | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| 28(2)/(4) | Written/electronic form (Art. 28(9)) | Click-through | Click-through/service agreement | Part of Agreement | Part of Agreement | Per Elastic | Account-based |
| Ch. V | Valid transfer basis | DPF/SCCs | SCCs | SCCs/DPF | SCCs via Data Transfer Addendum | UNVERIFIED | EU provider |

Cells reading "not read in detail" mean the contract was summarised, not read clause by
clause. Action: someone with legal training reads each signed DPA against (a) to (h) and
records the result here with a link to the signed copy.

## 3. Law-firm requirements: § 43e BRAO and § 203 StGB

Sources: [§ 43e BRAO](https://www.gesetze-im-internet.de/brao/__43e.html) and [§ 203 StGB](https://www.gesetze-im-internet.de/stgb/__203.html) (read via a fetch tool that summarised, so quotes are short; read the statute itself before relying on this).

A lawyer who uses these services is disclosing client secrets (Mandantengeheimnisse, § 43a Abs. 2 BRAO) to service providers. Summary of § 43e BRAO:

1. **Abs. 1:** access to secrets is allowed only as far as necessary for the service.
2. **Abs. 2:** choose the provider carefully; end the cooperation immediately if the Abs. 3 requirements can no longer be met. Practical record: a short written selection note per processor (this document is the start).
3. **Abs. 3:** a **text-form contract** that (1) binds the provider to confidentiality and warns of criminal liability, (2) limits access to what the contract requires, (3) states whether the provider may use other persons, who are then also bound in text form. Check each DPA or add a confidentiality addendum; **none of the public DPAs read here was checked for this confidentiality-with-criminal-liability warning, so assume it is missing until shown otherwise** (UNVERIFIED).
4. **Abs. 4:** for services performed **abroad**, access is allowed only if protection comparable to German secrecy protection exists, unless the nature of the secrets does not need it. This weighs against non-EU processing (Gemini data cached "in any country", US support affiliates at Langfuse). Whether US/Asian access meets "comparable" is a legal call (open item 3).
5. **Abs. 5:** where the service directly serves one mandate, the **client's consent** is required for access to third-party secrets.
6. **Abs. 6:** Abs. 2 and 3 still apply even with consent unless the client waives them expressly.
7. **Abs. 8:** data protection law stays applicable alongside (so this checklist and § 43e both apply).

§ 203 StGB: disclosure of a secret by a lawyer (Abs. 1 Nr. 3) is a crime unless a justification applies. Abs. 3 says it is **not** a disclosure when the lawyer shares secrets with "sonstige mitwirkende Personen" insofar as necessary for using their services. Abs. 4: the lawyer is liable if they fail to ensure that such a person was bound to secrecy, and the contributor is liable if they pass the secret on to a further helper without binding that helper. Consequence for the app: each processor (and in practice its sub-processors) needs a written secrecy obligation; masking reduces what is disclosed and so reduces the exposure, but does not remove the need, because NER can miss names and the clause content itself may be a client secret even when no person is named.

Related, outside this research: the BRAK has published guidance on cloud/IT service providers; I did not retrieve it, check [brak.de](https://www.brak.de) for the current text.

## 4. Technical and organisational measures in the app

Mapped from `docs/Phase 9 Design.md` and `configs/app.yaml`. "Status" is the shipped config, not the code.

| Measure | What it does | Art. | Status in shipped config |
| --- | --- | --- | --- |
| Masking boundary (Presidio + German spaCy NER + regexes) before every Gemini, Jina and Langfuse call; `MaskedText` type check in tests | Data minimisation, pseudonymisation of what leaves the server; supplementary measure for transfers | 5(1)(c), 25(1), 32(1)(a), 44-46 | **Off** (`privacy.masking.enabled: false`, `required: false`) |
| Per-text numbered placeholders; map in memory only, never stored or logged | Prevents the pseudonymisation key from becoming stored personal data | 25(1), 32(1)(a) | with masking |
| Reference-counted deletion in one transaction, outbox for blobs, Langfuse, checkpoints; idempotent retries | Erasure on request, incl. external processors | 17 (incl. 17(2) as far as processors are concerned), 19 | Code present; exercised by user/admin delete |
| Tombstones (id hash, time, reason, row counts, no content) in an append-only table | Accountability without keeping content | 5(2), 24, 30 | Present |
| Append-only `decision_events` with a narrowly scoped erasure exception | Integrity of the audit trail vs erasure | 5(1)(f), 17, 32 | Present (migration 006) |
| Retention job (files, reviews, traces) through the same deletion; nightly orphan sweep | Storage limitation | 5(1)(e), 25(2) | **Off** (`retention.enabled: false`, `files_days`/`reviews_days` null; `traces_days: 30`) |
| EU S3-compatible storage with server-side encryption (`AES256`), conditional writes (immutable blobs), proxy download default | Encryption at rest, EU location, access via login | 32(1)(a), 44 | **Off** (`files.backend: local`); needs provider choice |
| OIDC login, owner on every document/review, 404 not 403 for foreign ids, per-owner document ids | Access control, confidentiality, no cross-user leak via content hash | 25(2), 32(1)(b), (2) | **Off** (`auth.enabled: false`) |
| Credentials via environment only | Secrets not in git | 32 | Present |
| Rate limit, CORS allow-list, upload size limit | Abuse and availability | 32(1)(b) | Present |
| Shared-row design (content-addressed cache) with owner separation | Minimises duplicated personal data, avoids cross-owner exposure | 25(1) | Present |
| Backups: relying on provider PITR window | Deleted rows leave backups when the window ends | 5(1)(e), 17 | **Window not yet recorded** (open item) |
| Cache key = hash of unmasked text, never sent out | Hash stays on server | 25 | Present |

Gaps to note: TLS, DB encryption at rest and key management are provider-side and should be confirmed in each DPA's TOMs; there is no per-field encryption in the app; no data-subject request workflow beyond document deletion (Art. 15, 16, 20 requests about *third parties named in client contracts* are handled by the law firm, not the app, but the firm needs to know where the data lives, see the table in section 1).

## 5. Record of processing activities, draft (Art. 30)

Source: [Art. 30 GDPR](https://eur-lex.europa.eu/eli/reg/2016/679/oj). The law firm is the controller and AGB-Check operates as its tool; if the operator of AGB-Check is a separate party providing it to the firm, then that operator is the firm's processor and also needs its own Art. 30(2) record. Fields in `[...]` must be filled by the user.

**Controller entry (Art. 30(1)), draft**

| Field | Draft |
| --- | --- |
| Controller / representative / DPO | [law firm name, address, DPO if any] |
| Purpose | Legal review of general terms and conditions (AGB) submitted by clients, under German AGB law (§§ 305 ff. BGB), by lawyers of the firm, in performance of the mandate |
| Legal basis (Art. 6) | Art. 6(1)(b) (mandate/contract with client) for client data; Art. 6(1)(f) for third persons named in client documents (managing directors, contacts). **Legal call, open item.** Art. 9 data is not intended; if a contract contains special-category data, Art. 9(2)(f) (legal claims) might apply, **legal call**. |
| Categories of data subjects | Client staff and officers; contact persons and signatories named in contracts; sole traders; the firm's own users (lawyers) |
| Categories of personal data | Names, business addresses, e-mail, phone, IBAN, commercial register numbers appearing in contract text; filenames; lawyers' notes and decisions; OIDC subject and display name of users; IP address (rate limiting) |
| Recipients / processors | Google (Gemini), Elastic/Jina (embeddings), Qdrant, Neon/Databricks (Postgres), Langfuse/ClickHouse (traces), [S3 provider], [IdP], Gotenberg only if hosted by a third party. Masked data only for Google, Jina, Langfuse; Qdrant for vectors |
| Third-country transfers | Possible: Google (global facilities), Langfuse sub-processors (US/Asia support), Jina/Elastic (**UNVERIFIED**), Qdrant (limited US). Safeguards: SCCs/DPF, masking. See section 1 |
| Retention periods | [to be set by the firm]; defaults in config: traces 30 days; files and reviews no automatic deletion until set; backups per Neon PITR window [to be recorded] |
| TOMs (Art. 32) | Section 4 of this document |

**Processor entry (Art. 30(2)), if applicable:** controller [firm], categories of processing (storage, analysis with masked calls to AI models, export), transfers as above, TOMs as section 4.

## 6. Open items for the user (legal calls, not engineering)

Not legal advice: each of these needs a lawyer's or the data protection officer's decision.

1. **Switch-on decision and masking sign-off.** Masking, retention, auth and EU storage are off in `configs/app.yaml`. Decide when each goes on and set `masking.required: true`. Residual NER misses remain a risk; is masking sufficient for § 203 StGB purposes, or is client consent (§ 43e Abs. 5 BRAO) also needed?
2. **Retention periods.** `files_days` and `reviews_days` are null. Professional-law retention of working papers (e.g. § 50 BRAO, 6 years for lawyers' files, to be verified by counsel) against Art. 5(1)(e). Also Neon's backup window and traces (30 days).
3. **Gemini and non-EU access.** The Gemini API gives no EU-only guarantee in the terms read, and Langfuse's sub-processor list includes non-EU affiliates. Does that satisfy § 43e Abs. 4 BRAO ("comparable protection abroad") and Art. 44 ff., or move to Vertex AI EU / self-hosted Langfuse? A transfer impact assessment for Google and Langfuse is needed.
4. **Choice of EU object-storage provider** (Hetzner, Scaleway, OVH, other) and whether client-side encryption is required; also whether a US-owned provider's EU region is acceptable given CLOUD Act exposure.
5. **Confidentiality undertakings (§ 43e Abs. 3, § 203 Abs. 4).** The public DPAs read do not show the required criminal-liability warning and text-form secrecy undertaking; request an addendum from each processor or decide on an alternative.
6. **Is a DPIA required (Art. 35)?** AI-assisted analysis of contracts containing personal data of third parties, at scale, new technology: arguably triggers it; check the supervisory authority's "must-do" list (DSK Muss-Liste) for your Land.
7. **Jina/Elastic and Neon/Databricks contracts.** Identify which DPA is in force and obtain sub-processor and region commitments in writing (rows 2 and 4 are mostly UNVERIFIED).
8. **Legal basis and information duties.** Art. 6(1)(b)/(f) for third persons named in client documents; whether Art. 14 information is owed to them or an exception (Art. 14(5)) applies.
9. **Roles.** Is the operator of AGB-Check the firm itself, or a separate provider that is the firm's processor (and then subject to Art. 28 and § 43e itself)?
10. **Data-breach and response procedure** across processors: only Qdrant gives a fixed 48 h; Google and Langfuse give "promptly". Define the firm's Art. 33 internal process.
11. **Langfuse corrections dataset** (`corrections.enabled: false`): contract text is intentionally not exported until masking is on; decide whether it may ever be enabled for client data.

## Source list (all fetched 2026-11-17 unless noted)

- Gemini API terms: https://aistudio.google.com/docs/terms
- Google processor terms: https://business.safety.google/processorterms/
- Qdrant DPA: https://cloud.qdrant.io/dpa; cloud regions: https://qdrant.tech/documentation/cloud/create-cluster/
- Langfuse DPA (ClickHouse): https://clickhouse.com/legal/agreements/data-processing-addendum; sub-processors: https://clickhouse.com/legal/agreements/langfuse-subprocessors; regions: https://langfuse.com/security/data-regions
- Neon regions: https://neon.com/docs/introduction/regions; Neon schedule: https://neon.com/dpa; Databricks sub-processors: https://www.databricks.com/legal/databricks-subprocessors
- Jina legal: https://jina.ai/legal
- Hetzner data protection FAQ: https://docs.hetzner.com/general/general-terms-and-conditions/data-privacy-faq; Scaleway: https://www.scaleway.com/en/object-storage-signup/, https://www.scaleway.com/en/subprocessorlist/
- § 43e BRAO: https://www.gesetze-im-internet.de/brao/__43e.html; § 203 StGB: https://www.gesetze-im-internet.de/stgb/__203.html
- DPF litigation: https://www.wilmerhale.com/en/insights/blogs/wilmerhale-privacy-and-cybersecurity-law/20251201-european-court-of-justice-to-review-challenge-to-eu-us-data-privacy-framework
- GDPR text: https://eur-lex.europa.eu/eli/reg/2016/679/oj

Items drawn only from search-result summaries (not read on the vendor page): Hetzner locations and DPA template URLs, Scaleway DPA, Databricks DPA/Data Transfer Addendum, Langfuse region list, DPF appeal status, EDPB guideline contents. Items not found at all: Gemini API EU-only processing, Jina/Elastic region and sub-processors, Neon PITR window, OVH DPA, Gotenberg deployment.
