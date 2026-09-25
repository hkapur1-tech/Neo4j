# Hybridisation Strategy — Executive Summary

**Harshit Kapur** · MSc CS, Lakehead University · Supervisors: Dr. Sabah Mohammed, Dr. Jinan Fiaidhi

**The problem.** Clinical questions split in two. *Factual* ones — which drugs during the admission where creatinine peaked, how many readmissions, did the diagnosis change — have exact answers in structured fields. Semantic search cannot count, cannot order by time, and cannot guarantee completeness. *Interpretive* ones — why venetoclax rather than continued prednisone, what the team thought was driving the renal decline — exist only in free-text notes, where no database column holds a clinician's reasoning. One retrieval mechanism cannot serve both.

**Why the earlier approach failed.** The benchmark hybrid combined BM25, FAISS and an LLM-extracted knowledge graph. On MedQA-USMLE it scored **96.2%** against a **96.0%** no-retrieval baseline — McNemar **p = 0.72**, indistinguishable. The diagnosis matters more than the number: all three components were doing **fuzzy similarity matching**. BM25 matched words, FAISS matched embeddings, and the graph — built by prompting an LLM over the same text — matched concepts the LLM had already associated. Three views of one signal; combining them added noise, not information. Hybrid retrieval did help on PathVQA (**68.0% → 79.6%, p < 0.001**), where parametric knowledge was genuinely insufficient. That identifies the condition under which retrieval pays, but neither benchmark contains longitudinal records, so neither tested the factual/interpretive split.

**The revised architecture.** Two paths that fail in non-overlapping ways, **routed by question type rather than fused by score**.

| | **Path A — Structured** | **Path B — Narrative** |
|---|---|---|
| Method | Text2Cypher → Neo4j traversal | Dense vector search over note embeddings |
| Source | Admissions, diagnoses, medications, labs | Discharge summaries, progress notes |
| Answers | *what, when, how many, in what order* | *why, what was considered, what was ruled out* |
| Guarantees | Exact, complete, **auditable** | Approximate, ranked, no completeness guarantee |
| Fails at | Anything not in a column | Counting, ordering, exhaustive enumeration |

Path A's output is **verifiable**: a Cypher query is a written artifact a clinician can read and confirm asked the right question. A similarity score cannot be audited this way — clinically, that is the difference between a reviewable system and an opaque one. Routing happens *before* Cypher is generated, and that ordering is load-bearing: Text2Cypher does not refuse. Given *"why was therapy changed?"* it produces plausible Cypher returning related-but-irrelevant rows, and an answer built from them reads as responsive while being unfounded. A classifier sends interpretive questions to the narrative path first. Composite questions (*"why did therapy change after the admission where creatinine peaked?"*) run Path A first to identify the admission, then Path B scoped to that admission's notes — the graph narrows the search space before the vectors are consulted.

**Evidence so far.** A working Neo4j property graph on the openly-licensed MIMIC-IV demo: **33,980 nodes, 45,226 relationships** across Patient, Admission, Diagnosis, Procedure, Medication and LabEvent — roughly six times MediGRAF's 5,973 nodes, over ten times the patients. Text2Cypher translates natural-language clinical questions to executable Cypher against it. Representative result: patient 10014354 traced across **20 admissions**, returning in one query the CLL status, therapy and peak creatinine at each visit — creatinine at a 1.3–1.4 baseline before any CLL therapy, rising to 2.2 across four admissions on prednisone, then falling back through 1.9 and 1.5 to 1.3 after a switch to venetoclax — with a documented remission and a subsequent return to active disease. The two drugs never co-occur, so the record shows a switch rather than a taper. The association is temporal only; the graph carries no evidence of cause. No amount of passage retrieval produces that table — and what the graph *cannot* say is **why** the therapy changed. That is Path B's job, and it is the whole argument in one query.

**How this differs from MediGRAF.** Thio et al. build the same two ingredients — Text2Cypher over Neo4j, plus embeddings over discharge summaries and radiology reports — but combine them by running both paths *simultaneously* and merging the results through **context concatenation**, with no routing step and no validation of the generated Cypher. That is the same fusion strategy that flattened the MedQA result above. Their reported accuracy is 80% on simple queries and 51.6% on medium ones, over a graph of 5,973 nodes across ten patients.

Routing is therefore not a stylistic variation on their design; it is a direct response to a negative result this project already produced. The hypothesis to test is that **routing by question type outperforms concatenating both contexts**, because concatenation dilutes an exact structured answer with approximate passages whenever the question was structural to begin with. A read-only validator on the generated Cypher is a second difference, and a prerequisite for running this against real records at all.

**Where this stands.** Path A is implemented and demonstrated on the open demo release. Path B needs MIMIC-IV-Note, which the demo does not include; PhysioNet credentialing is now granted, and their guidance permits processing credentialed data through providers that do not retain or train on prompts, so the existing pipeline runs on it unchanged. Routing is implemented but not yet measured: the experiment — routing against context concatenation, scored on the simple and medium tiers — is the immediate next step.

---

*Route factual questions to an auditable graph query and interpretive questions to note retrieval, rather than fusing similarity scores from mechanisms that all measure the same thing.*
