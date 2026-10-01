# Results

**Harshit Kapur** · MSc CS, Lakehead University · Supervisors: Dr. Sabah Mohammed, Dr. Jinan Fiaidhi

All figures below are read from a live Neo4j AuraDB instance rebuilt from the
notebook, and from evaluation runs saved as JSON in this repository. Nothing is
estimated. Where a number moved during development, the correction is recorded
rather than quietly replaced.

Everything in this document derives from the **MIMIC-IV Clinical Database Demo
v2.2** (ODC-BY 1.0, open) for the graph, and from **MIMIC-IV-Note v2.2**
(credentialed) for the note corpus. No credentialed record or derived output is
committed to this repository.

---

## 1. The graph

```
33,980 nodes · 45,226 relationships
100 patients · 275 admissions
```

| Node label | Count | | Relationship | Count |
|---|---:|---|---|---:|
| LabEvent | 31,183 | | INCLUDES_LAB | 31,183 |
| Diagnosis | 1,472 | | HAS_MEDICATION | 8,540 |
| Medication | 598 | | HAS_DIAGNOSIS | 4,506 |
| Procedure | 352 | | HAS_PROCEDURE | 722 |
| Admission | 275 | | HAS_ADMISSION | 275 |
| Patient | 100 | | | |

For comparison, MediGRAF (Thio et al., *Frontiers in Digital Health*, 2026)
reports 5,973 nodes and 5,963 relationships over ten patients — roughly a sixth
the size over a tenth the cohort.

**Dates are shifted.** MIMIC-IV de-identifies by moving every patient's record
forward by a random per-patient offset, applied consistently. Years in the
2140s and 2150s are therefore not real dates. Intervals are preserved exactly,
so all temporal reasoning in this work is valid; only absolute dates are
meaningless. `Patient` carries `anchor_age` (the true age at a reference point)
and `anchor_year` rather than a date of birth.

Only labs flagged `abnormal` are stored, so a `LabEvent` count is a count of
abnormal results, not of tests performed. `Diagnosis`, `Procedure` and
`Medication` nodes are shared across patients; admission-specific detail
(dose, route, sequence) is carried on the relationships.

### 1.1 Two data-integrity faults found and fixed

Both were found by re-querying the live database against the written
documentation rather than by inspection, and both had already propagated into
draft figures before they were caught.

**A doubled lab load.** `LabEvent` stood at 62,366 — exactly twice 31,183. The
loader uses `CREATE` for `LabEvent` (lab results are not uniquely keyed, so
`MERGE` is unavailable), which is not idempotent, and the cell had been run
twice. The graph was wiped, reloaded once, and re-verified. Totals in earlier
drafts (65,163 nodes / 76,409 relationships) were wrong and have been corrected
throughout.

**663 NaN-valued properties.** pandas `NaN` was written through to Neo4j as a
float NaN rather than as null: 621 on `LabEvent.valuenum` and 42 on
`Admission.discharge_location`. NaN compares unequal to everything, including
itself, so `WHERE x IS NULL` does not match these and any query about missing
values returns the wrong answer silently. The 621 lab cases are all
qualitative blood-smear labels (Poikilocytosis, Ovalocytes, Anisocytosis,
Polychromasia and similar) which report text rather than a number.

Creatinine — the measure used in every trajectory figure — was checked
specifically: **1,072 events, zero NaN, zero null.** No reported creatinine
value is affected.

The properties were removed and the loaders now coerce with
`df.astype(object).where(df.notna(), None)` before `to_dict('records')`. The
pandas-to-Neo4j type boundary produced two distinct failures in this project:
`MERGE` rejects a NaN relationship property outright, while `SET` accepts one
silently. The loud failure is the safer one.

---

## 2. The note corpus

Retrieved from MIMIC-IV-Note v2.2 through PhysioNet's sanctioned BigQuery
access, scoped to the 100 demo `subject_id`s.

| | |
|---|---:|
| Discharge summaries | 243 |
| Patients with at least one note | 100 / 100 |
| Admissions with a discharge summary | 243 / 275 (88%) |
| Median note length | 10,750 characters |
| Chunks indexed (1,200 chars, 200 overlap) | 3,035 |

The 32 admissions without a discharge summary are observation and same-day
encounters — 24 EU observation, 3 ambulatory observation, 3 direct observation,
1 direct emergency, 1 surgical same-day — which do not generate one in
MIMIC-IV. Note coverage is therefore complete for inpatient admissions, and
the gap is a property of the record rather than of the extraction.

This matters for routing: an interpretive question about an observation stay
has no note to answer from, and the correct behaviour is to return nothing
rather than to retrieve a neighbouring admission's note.

Embeddings are computed locally (`BAAI/bge-small-en-v1.5`, 384 dimensions), so
credentialed note text never leaves the execution environment. At 3,035 chunks,
exact cosine search is used; no vector database is required.

---

## 3. Path A — Text2Cypher over the graph

### 3.1 Question bank

44 questions in five tiers. The simple and medium tiers follow MediGRAF's
naming so the figures sit next to theirs; the others are additions.

| Tier | n | What it tests |
|---|---:|---|
| simple | 12 | single-hop lookups and aggregates |
| medium | 12 | multi-hop traversal, filtering, ordering |
| hard | 10 | negation, absence, set difference, duration arithmetic, cross-admission comparison |
| narrative | 6 | router only — not answerable from structure |
| adversarial | 4 | router only — surface form points at the wrong path |

Three questions are **negative cases** where the correct answer is no rows
(no sepsis diagnosis; prednisone and venetoclax never co-prescribed; no
patient with admissions but no medications). These test whether the system
invents records that do not exist.

### 3.2 Routing

Router accuracy was 44/44 across all five tiers, including the four
adversarial questions whose phrasing points the wrong way.

**This is reported as a sanity check, not as an accuracy figure.** The
questions were written while looking at the router's own prompt, so the test
and the artifact under test share an author — a construct-validity problem a
reviewer would be right to raise. At n = 44 with no errors there is also no
variance to characterise. A defensible router figure requires questions
written by someone who has not seen the prompt.

### 3.3 Text2Cypher accuracy — two scoring criteria

Five runs, temperature 0. Mean with observed range.

| Tier | n | strict | projection |
|---|---:|---|---|
| simple | 12 | 91.7% (92–92) | 100.0% (100–100) |
| medium | 12 | 25.0% (25–25) | 91.7% (92–92) |
| hard | 10 | 38.0% (30–40) | 86.0% (80–90) |

- **strict** — the returned value multiset must match the reference exactly.
  Extra columns fail.
- **projection** — some subset of the result's columns, renamed, must reproduce
  the reference rows exactly. Extra columns pass.

Both criteria are defensible. The medium tier scores **25.0% or 91.7% over
byte-identical model outputs**, both with zero range across five runs: a
67-point gap that is entirely a scoring choice, not sampling noise.

The mechanism is specific. On multi-hop questions the model returns the
requested values *plus* context — `hadm_id`, the peak lab value, the ICD code,
the route and dose. Asked which medications were given during the admission
where creatinine peaked, it returns the 52 correct medications and also the
admission id and the peak value of 2.2. That is a more useful answer than the
reference, and strict scoring marks it wrong. On simple aggregates there is
nothing extra to add, which is why the two criteria nearly agree there.

**An extra column is not always inert.** In an aggregating query it joins the
grouping key and can change the result: adding `d.icd_code` to a
"most frequent diagnoses" query split the ICD-9 and ICD-10 codings of
*Acute kidney failure, unspecified* into two smaller counts and changed the
fifth-place row. Projection scoring correctly fails that case.

**The comparator hits the same wall.** MediGRAF reports 80.0% on simple and
51.6% on medium for its Cypher-only configuration, with precision, recall and
F1. For its hybrid configuration it reports 100% on both tiers — but precision
and F1 are marked N/A, because, in their words, *"the hybrid system augments
responses with contextual information from unstructured sources, producing
enriched outputs that extend beyond the discrete ground-truth format required
for precision measurement."*

That is this section's finding, in the comparator's own table. Enriched output
cannot be scored by exact match. They resolved it by reporting recall alone;
this work resolves it by reporting two criteria side by side. Either is
defensible. What is not defensible is comparing a recall-only figure with a
precision-and-recall figure as though they measured the same thing — and that
is what the 80% → 100% improvement does.

### 3.4 Failures

Two questions fail in every run.

**Negation defeated by the vocabulary (M05).** Asked on which admission
leukaemia was recorded as *in remission*, the model filters
`toLower(d.long_title) CONTAINS 'leukemia' AND CONTAINS 'remission'`. The
active-disease title is *"Chronic lymphocytic leukemia of B-cell type not
having achieved remission"* — which contains the substring `remission`. The
filter matches both states and returns all 20 admissions instead of 2. The
query is syntactically valid, semantically plausible, and confidently wrong.

The trap is in the ICD vocabulary, not the question. The inverted question
(H01: *"recorded as active — that is, not in remission"*) names the
distinction and passes. So the failure is not an inability to express
negation; it is an inability to discover a negation the question did not
signal. Clinically this is the important case: a system reporting "no active
leukaemia" for a patient with 19 such admissions fails silently, and only a
reviewable query exposes it.

**Undocumented property type (H08).** Asked for elapsed days between first and
last admission, the model writes `datetime(first_admit)` against
`'2146-10-08 23:47:00'`. Neo4j requires a `T` separator and throws. The schema
prompt lists `admittime` as a property but does not say it is a string in that
format. This failure mode is benign: invalid Cypher errors loudly, in contrast
to M05, which runs clean and returns ten times too many rows.

**One question is unstable** at temperature 0: H09, the cross-admission
comparison, passes in 3 of 5 runs. With n = 10 per tier, one unstable question
moves the hard-tier figure by 10 points — which is why a single run with no
range is not a measurement.

### 3.5 Ablation — documenting property types in the schema prompt

H08 suggests an obvious fix: state the property type. Two formulations were
tested, five runs each, against the unmodified baseline.

| | A — no note | B — four lines | C — one line |
|---|---|---|---|
| simple (projection) | **100.0%** | 100.0% | 83.3% |
| medium (projection) | **91.7%** | 91.7% | 91.7% |
| hard (projection) | **86.0%** | 85.0% | 84.0% |
| H08 (the target) | 0% | **100%** | **100%** |
| H01 (negation) | **100%** | 25% | 20% |
| H09 (comparison) | 60% | 25% | 40% |
| S06, S07 (min/max date) | **100%** | 100% | 0% |

Both formulations fixed the target completely and neither improved the tier.
Condition B broke H01 — a negation question mentioning no dates, which the
added text does not address. Condition C broke it too, and additionally
destroyed the two simplest date questions by making the model convert dates on
queries needing no arithmetic, returning a temporal type where the reference
returns the stored string.

Since the regression appears under both a four-line and a one-line
formulation, it is not an artifact of prompt length. The mechanism is not
established.

> **Schema-prompt edits have non-local effects that do not scale with the size
> of the edit. Validating a prompt change on the question it was written for is
> unsafe.**

Condition A is the reported configuration. H08 remains an open failure, and
the ablation is the evidence that the obvious fix does not pay for itself.

---

## 4. Path B — dense retrieval over discharge summaries

Retrieval works, and the illustrative case is the one the graph cannot answer.
Asked *"Why was venetoclax started instead of continuing prednisone?"*, scoped
to patient 10014354, the retrieved passages include:

```
[0.710] admission 26486158
Chief Complaint: Initiation of venetoclax
```

That passage carries the justification for the therapy change. No graph query
produces it, because no node or property holds a clinician's reasoning.

**It ranks third.** Above it sit a urinary-retention paragraph (0.759) and a
fluid-management paragraph (0.750), neither responsive to the question. That
single observation generalises, and the measurement below puts a number on it.

### 4.1 Retrieval accuracy

Structural questions have reference row sets; interpretive ones do not. The
reference was therefore built from the notes: sample a passage, have the model
write a *why* question whose answer lies in it, and keep that passage as the
target. Retrieval is then scored as an information-retrieval task — does the
top-k set contain the passage the question was written from — rather than as a
claim about clinical correctness. This is the construction used for
SQuAD-style benchmarks; its weaknesses are listed below.

40 questions were generated and **33 retained**. Seven were removed because
the gold passage did not contain the answer: five were note headers
(`Name:`, `Unit No:`, `Admission Date:`, `Allergies:`) from which the model
wrote a question about the note rather than the passage, and two had the
answer in a different passage that retrieval correctly found instead.

| | unscoped | scoped to the patient |
|---|---|---|
| recall@1 | 9.1% | 36.4% |
| recall@3 | 12.1% | 72.7% |
| recall@5 | 15.2% | **87.9%** |
| recall@10 | 30.3% | 97.0% |
| correct note in top 5 | 33.3% | **100%** |
| MRR | 0.128 | 0.570 |
| never retrieved | 23 / 33 | 1 / 33 |

*Unscoped* searches all 3,035 passages. *Scoped* searches only the passages
belonging to the question's patient — a mean pool of 24 — which is what the
composite path does once Path A has identified the patient or admission.

**Scoping raises recall@5 by 72.7 points.** That is the measured value of
letting the graph narrow the search space before retrieval runs, and it is the
largest effect in this work. It is a finding about retrieval rather than an
advantage over any particular architecture — patient-level scoping is equally
available to a system of disconnected per-patient subgraphs, and MediGRAF does
not report doing it. What is not available to such a system is scoping by a
*cross-patient* traversal, since the entities that would make one possible are
copied per patient rather than shared.

**Unscoped retrieval is weak**, and that is reported rather than hidden: over
a corpus of this size a general-purpose embedding model finds the responsive
passage in the top five only 15% of the time. A clinical-domain embedding
model and a reranking step are the obvious remedies, and neither is
implemented here.

**The generated questions are not winning by copying their source.** Mean
lexical overlap with the gold passage was 0.28, and the questions with the
highest overlap scored **14.7 points worse** scoped than the rest — the
opposite of the inflation this construction is usually vulnerable to.

**Known weaknesses of this reference.** Every question is answerable by
construction, so the bank contains no unanswerable interpretive questions — in
particular none about the 32 admissions with no discharge summary. Chunk
boundaries are arbitrary, which is why the note-level score is reported
alongside the exact-passage one. And the questions are machine-written and
filtered by the author, not by a clinician.

---

## 5. Routing versus context concatenation

The architectural claim under test: routing a question to one retrieval path
beats running both and concatenating their contexts, because concatenation
dilutes an exact structured answer with approximate passages whenever the
question was structural to begin with.

Both arms use the **same synthesis prompt and the same model**, and are scored
by the same criterion; the only difference is what enters the context window.
The comparison therefore isolates context composition, not retrieval quality.
MediGRAF's own hybrid-versus-Cypher-only comparison cannot do this: its two
arms are scored by different metrics, because concatenated output is not
exact-matchable.

- **Arm R (routing)** — question → router → Path A only → answer.
- **Arm C (concatenation)** — question → both paths unconditionally → merged
  context → answer. No routing, no scoping: the fusion design implemented
  faithfully rather than as a straw man.

Scored over the 32 structural questions with a reference row set, by **value
recall** — the fraction of reference values surviving into the answer text.
This needs no human annotation and no clinical judgement.

| | mean value recall |
|---|---:|
| routing | 0.744 |
| concatenation | 0.744 |

| mean absolute per-question difference | |
|---|---:|
| routing versus itself, re-run unchanged | 0.244 |
| routing versus concatenation | 0.098 |

**No effect is detectable.** The difference between architectures is smaller
than the answer-synthesis step's own run-to-run variance, so at this sample
size the comparison cannot support a claim about architecture in either
direction. The hypothesis is not supported.

This comparison ran at the API's default sampling temperature, not at
temperature 0. A temperature-0 replication is the obvious next step and was
not performed.

### 5.1 One instance of the predicted failure

The aggregate is null; the mechanism is nonetheless observable. Asked which
patients have a leukaemia diagnosis, ranked by total admissions:

> **Routing** — "subject_id 10014354 with 20 total_admissions, then 10035631
> with 9, then 10019003 with 8." Correct.
>
> **Concatenation** — "Only one of the supplied notes documents a leukaemia
> diagnosis: admission 21476294… The context does not link admission 21476294
> to a specific subject_id, so the leukaemia case cannot be assigned."

The correct rows were present in the concatenated context. Five note passages
pulled the answer entirely onto the notes, and it concluded the question could
not be answered. This is reported as an illustration of what routing prevents,
**not** as evidence of an aggregate effect — the measurement above shows there
is none at this sample size.

---

## 6. What the evidence supports

**Supported.** A longitudinal clinical property graph on openly-licensed data,
six times the size of the closest published comparator over ten times the
cohort, rebuildable from the notebook and verified against the live database.
Text2Cypher over it reaching 100% on simple and 91.7% on medium under
projection scoring, with ranges over five runs. Dense retrieval over 243
discharge summaries recovering clinical reasoning the graph cannot express.

**Not supported.** That routing outperforms context concatenation. The
measured difference is below the noise floor of the synthesis step.

**Argued rather than measured.** That routing is preferable for
*auditability* and *context economy*. A generated Cypher query is a written
artifact a clinician can read and confirm asked the right question; a
similarity score cannot be inspected this way. M05 is the concrete case — a
query that runs clean and returns ten times too many rows is caught by reading
it, and by nothing else. Neither argument requires an accuracy advantage, and
none is claimed.

Both correspond to failure modes MediGRAF reports. Cross-patient retrieval
overflowed their context window; because this graph shares clinical entities
across patients rather than holding disconnected per-patient subgraphs, the
same question resolves to an aggregation returning a handful of rows. And they
state that context flattening *"complicates precise source attribution"*, with
the model *"struggl[ing] to map a specific sentence in the output back to its
precise origin node ID"* — listing citation-aware generation as future work.
Routing supplies path-level provenance by construction.

**The argument against routing, which this work cannot refute.** MediGRAF's
Cypher-only configuration failed on 20% of *simple* queries *"primarily due to
gaps in structured coding where clinical signals existed only in free-text
narratives"*, and they concatenate everywhere to stay robust to that. Real
records are incompletely coded, and a question that looks structural may not be
answerable from structure. Concatenation covers that case; routing does not,
except through the step-3b fallback, which triggers only on an empty result and
not on a non-empty but incomplete one.

**Findings about evaluation, which are the more transferable result.**

1. The scoring criterion moves medium-tier Text2Cypher accuracy by 67 points
   over identical outputs, with zero variance under both criteria. Published
   figures that do not state a criterion are not comparable.
2. Schema-prompt edits have non-local effects that do not scale with edit
   size. A prompt change validated only on its target question can degrade
   unrelated ones.
3. Run-to-run variance in answer synthesis exceeds the difference between the
   two retrieval architectures compared here. Single-run architecture
   comparisons on question sets of this size measure noise.

---

## 7. Limitations

- **n is small.** 12 questions per tier for simple and medium, 10 for hard, 32
  for the routing comparison. One question is 8–10 points.
- **The question bank is self-authored**, including the router's. This is a
  construct-validity limitation, most acute for the 44/44 router figure.
- **The routing comparison ran at default temperature**, unlike the Path A
  evaluation, which ran at temperature 0.
- **Projection scoring ignores row order** except on the three questions
  explicitly marked as ordered.
- **The reference queries are hand-written** and were themselves a source of
  error during development: two questions scored the model wrong for choosing
  a more natural reading than the reference encoded, and were rewritten
  (S12: "most frequently recorded" did not state whether to count
  relationships or distinct admissions; M10: "ranked by number of admissions"
  did not state whether that meant the patient's total).
- **Embedding model and chunking strategy are unmeasured choices.** Fixed-size
  chunking ignores the section structure discharge summaries have; a
  clinical-domain embedding model was not compared against the general one.
- **One patient carries the worked example.** Patient 10014354's trajectory is
  illustrative, not representative.
- **The routing comparison cannot detect the benefit MediGRAF reports.** Every
  question in the bank has a reference Cypher query that returns the answer, so
  the structured data always contains it. The case they identify — where the
  fact exists only in free text — is excluded by construction. The null result
  is therefore conditional on structural completeness, and the experiment that
  would test the open question is one whose questions are *not* fully
  answerable from structure.
- **The question bank is smaller and has no clinical input.** MediGRAF used
  141 questions *"manually curated by three independent medical clinicians"*,
  with complex cases judged by two hospital physicians. This work uses 44
  self-authored questions and no clinical review, which is the most significant
  limitation here. Their paper does not define what curation involved, state
  how ground-truth answers were established for the scored tiers, or say
  whether the curators and the reviewers were the same people — so the
  comparison is between a documented weak process and an undocumented
  stronger one. The question bank, reference queries and scoring harness used
  here are in the repository and re-runnable.
- **Different models and releases.** MediGRAF used GPT-4o-mini and
  text-embedding-3-small (1,536d, HNSW) over MIMIC-IV v3.1; this work used
  Claude Opus and bge-small-en-v1.5 (384d, exact search) over the v2.2 demo.
  Accuracy figures are confounded by model choice independently of any scoring
  criterion.

---

## 8. Reproducing

1. MIMIC-IV Demo v2.2 from PhysioNet — open access, no credentialing.
2. A free Neo4j AuraDB instance. The notebook rebuilds the graph in about
   eight minutes.
3. `evaluation.py` — `verify_gold()` first (no API calls; it executes every
   reference query and reports errors), then `evaluate()`.
4. `experiment.py` — `eligible()`, then `run_experiment()`.
5. MIMIC-IV-Note v2.2 requires PhysioNet credentialing. Access is through
   BigQuery; the extraction query is scoped to the demo cohort.

Credentials are entered at runtime and never written to disk or committed.
Saved run files (`run_*.json`, `evaluation_results.json`,
`experiment_results.json`) contain question ids, generated Cypher and scores —
no patient data.
