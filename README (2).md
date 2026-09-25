# Figures

Paper-ready captions. Every figure is verified against the live graph — 33,980 nodes and 45,226 relationships over the MIMIC-IV Clinical Database Demo v2.2.

---

## Fig. 1 — Graph schema
`schema_clean.svg` · `schema_clean.png`

![Graph schema](schema_clean.png)

> **Fig. 1.** Schema of the clinical property graph. `Admission` is the hub: every clinical fact attaches to an admission rather than directly to a patient, which is what makes longitudinal queries possible — the graph records not only *that* a patient had a diagnosis but *when*, relative to every other event. Admission-specific detail is carried on the relationships rather than the nodes (Table I), so a drug is stored once and its dose, route and start time belong to the admission that used it.

Accompanying table, for the same page:

**TABLE I — Schema specification.** Counts read from the live graph. `*` uniqueness constraint, `†` index.

| Node label | Count | Key properties |
|---|---:|---|
| Patient | 100 | `subject_id`\* · gender · anchor_age · anchor_year |
| Admission | 275 | `hadm_id`\* · admittime · dischtime · admission_type · discharge_location |
| Diagnosis | 1,472 | `icd_code`\* · long_title · icd_version |
| Procedure | 352 | `icd_code`\* · long_title |
| Medication | 598 | `name`\* (lowercased) |
| LabEvent | 31,183 | `label`† · value · valuenum · valueuom · flag · charttime |

| Relationship | Count | Properties |
|---|---:|---|
| HAS_ADMISSION | 275 | — |
| HAS_DIAGNOSIS | 4,506 | seq_num |
| HAS_PROCEDURE | 722 | chartdate |
| HAS_MEDICATION | 8,540 | route · dose · unit · starttime |
| INCLUDES_LAB | 31,183 | — |

All `LabEvent` nodes carry `flag = 'abnormal'`; normal results were not loaded. `seq_num` is the diagnosis ordering within an admission, where sequence 1 is the principal diagnosis.

`schema_fixed.svg` is the same figure with every property drawn on it — too dense for a two-column layout, but the right version for the repository and for slides.

---

## Fig. 2 — Retrieval pipeline
`lucid_pipeline.png`

![Retrieval pipeline](lucid_pipeline.png)

> **Fig. 2.** Routed retrieval architecture. A classifier assigns each question to one of two paths before any query is generated. Structural questions (*what, when, how many, in what order*) are translated to Cypher, validated as read-only, and executed against the graph; interpretive questions (*why, what was considered, what was ruled out*) are routed to dense retrieval over clinical notes. Routing precedes generation because Text2Cypher does not refuse: given an unanswerable question it emits syntactically valid Cypher returning related-but-irrelevant rows, from which a fluent and unfounded answer is readily synthesised. A secondary fallback (3b) covers structural queries that legitimately return nothing. The executed Cypher is returned alongside the answer, so the retrieval step is auditable.

**Before use:** correct "PART B NARRATIVE" to "PATH B — NARRATIVE", and replace "blocked: MIMIC-IV-Note credentialing" with "requires MIMIC-IV-Note (credentialed release)".

---

## Fig. 3 — Patient journey
`lucid_patient_journey.png`

![Patient journey](lucid_patient_journey.png)

> **Fig. 3.** Therapy trajectory for patient 10014354, assembled in a single graph traversal. Of 20 admissions, four record prednisone and four record venetoclax; the two never co-occur on any admission, so the record shows a switch in August 2148 rather than a taper or a combination. Peak creatinine across these admissions sits at a 1.3–1.4 baseline before any CLL therapy is recorded, rises to 2.2 during the prednisone admissions, and falls back through 1.9 and 1.5 to 1.3 afterwards. A remission is coded in June 2149, with active disease again that September. **The association is temporal, not causal**; the graph carries no evidence of mechanism. Peak-per-admission is a coarse summary — the underlying series varies widely within each stay — and two admissions on therapy recorded no creatinine at all.

**Optional:** adding the peak creatinine under each node (2.2, 2.0, —, 2.2 │ 1.9, —, 1.5, 1.3) and a dashed divider at the switch would let the figure carry the finding rather than relying on the caption for it.

---

## Supporting material — not for publication

**Neo4j Browser exports.** The schema (`CALL db.schema.visualization()`) and the patient subgraph, exported from the live database. These are evidence that the graph is real and queryable, for the repository and for screen-sharing. They are not publication figures.

**Generated alternates.** `figure_journey.svg` and `figure_graph.svg` render the patient journey programmatically from the database, the first with the creatinine trajectory attached. `figure_pipeline.svg` is an alternate rendering of Fig. 2. All regenerate from the graph when the data changes.

---

## Provenance

All figures derive from the open demo release (ODC-BY 1.0). No credentialed MIMIC-IV record appears in any of them.
