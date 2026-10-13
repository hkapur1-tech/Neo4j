"""
dka_questions.py — question bank and scoring harness for the DKA cohort.

PURPOSE
  Gives the three schema arms (A hand-written, B Arrows, D Arrows + notes)
  something to be measured against. Same method as evaluation.py on the demo
  graph: a reference Cypher query per question, gold rows obtained by running
  that query once (no LLM involved), and the model's generated query scored
  against those rows under two criteria.

TIERS
  simple (8)   single-hop counts and lookups
  medium (8)   one join, an aggregation, or a filter
  hard   (8)   multi-hop, negation, ordering, or a derived comparison
  dka    (6)   specific to the recurrent-admission structure: interval
               between DKA episodes, severity trend, interleaved admissions —
               questions that have no equivalent on the demo graph, because
               the demo graph has no patient with enough admissions to ask
               about recurrence
  adversarial (4)
               each exploits one thing the schema cannot say by itself and
               the prose notes can: that Glucose spans two fluids, that
               is_dka is what makes "the cohort" rather than "all 1,979
               admissions", that seq_num distinguishes principal from
               secondary, and that 34 DKA admissions have no lab at all. A
               model given SCHEMA_B (no notes) is expected to do worse here
               specifically — that is the point of these four.

DYNAMIC QUESTIONS
  Three questions (H02, H07, X03) need a concrete subject_id or icd_code
  filled in before they have an answer — "the patient with the most DKA
  admissions" has to be looked up once. build_bank(run) resolves these
  against the live graph and bakes the concrete value into both the question
  text and the reference query, so the saved bank is static afterward and
  verify_gold() needs no further lookups.

SCORING
  Same dual criterion as evaluation.py: strict requires the exact value
  multiset in the model's own columns; projection allows extra columns, as
  long as some subset of them reproduces the reference exactly. Ported here
  rather than imported, because this bank runs against a different schema and
  a different database than evaluation.py's, and keeping the two independent
  means a bug in one cannot silently affect the other's numbers.

REQUIRES
  From dka_build.py  : run, SCHEMA  (exec that first; SCHEMA is arm A)
  From arrows.py     : SCHEMA_B, SCHEMA_D (built in the previous session)
  From the pipeline cell : llm(prompt, max_tokens)

USAGE
    exec(open('dka_questions.py').read())
    bank = build_bank(run)                       # resolves H02, H07, X03
    gold = verify_gold(bank, run)                 # no API calls

    results_A = run_arm(SCHEMA,   "A hand-written", bank, run)
    results_B = run_arm(SCHEMA_B, "B arrows",        bank, run)
    results_D = run_arm(SCHEMA_D, "D arrows+notes",  bank, run)

    report(results_A); report(results_B); report(results_D)
    compare_arms({"A": results_A, "B": results_B, "D": results_D})
"""

import json
import re
from itertools import permutations

import pandas as pd


# ===========================================================================
# THE BANK
# ===========================================================================
# `cypher` may contain {subject_id} or {icd_code} placeholders, resolved once
# by build_bank(). `ordered` marks a question whose row order is part of the
# answer (an ORDER BY the question asks for, not one added for determinism).

_RAW_BANK = [
    # ---- simple ------------------------------------------------------
    {"id": "S01", "tier": "simple", "ordered": False,
     "question": "How many patients are in the cohort?",
     "cypher": "MATCH (p:Patient) RETURN count(p) AS n"},
    {"id": "S02", "tier": "simple", "ordered": False,
     "question": "How many admissions are DKA admissions?",
     "cypher": "MATCH (a:Admission {is_dka: true}) RETURN count(a) AS n"},
    {"id": "S03", "tier": "simple", "ordered": False,
     "question": "How many admissions are there in total, across all patients?",
     "cypher": "MATCH (a:Admission) RETURN count(a) AS n"},
    {"id": "S04", "tier": "simple", "ordered": False,
     "question": "How many ICU stays are recorded?",
     "cypher": "MATCH (s:ICUStay) RETURN count(s) AS n"},
    {"id": "S05", "tier": "simple", "ordered": False,
     "question": "How many distinct medications were prescribed?",
     "cypher": "MATCH (m:Medication) RETURN count(m) AS n"},
    {"id": "S06", "tier": "simple", "ordered": False,
     "question": "How many lab events were recorded in total?",
     "cypher": "MATCH (l:LabEvent) RETURN count(l) AS n"},
    {"id": "S07", "tier": "simple", "ordered": False,
     "question": "What is the gender breakdown of the cohort?",
     "cypher": "MATCH (p:Patient) RETURN p.gender AS gender, count(p) AS n "
               "ORDER BY gender"},
    {"id": "S08", "tier": "simple", "ordered": True,
     "question": "What are the five most common admission types, "
                 "most frequent first?",
     "cypher": "MATCH (a:Admission) RETURN a.admission_type AS type, "
               "count(*) AS n ORDER BY n DESC LIMIT 5"},

    # ---- medium --------------------------------------------------------
    {"id": "M01", "tier": "medium", "ordered": False,
     "question": "What is the average number of admissions per patient?",
     "cypher": "MATCH (p:Patient)-[:HAS_ADMISSION]->(a:Admission) "
               "WITH p, count(a) AS n RETURN avg(n) AS mean_admissions"},
    {"id": "M02", "tier": "medium", "ordered": True,
     "question": "Which five medications were prescribed in the most DKA "
                 "admissions, most frequent first? Count each admission once "
                 "per medication.",
     "cypher": "MATCH (a:Admission {is_dka: true})-[:PRESCRIBED]->(m:Medication) "
               "RETURN m.drug AS drug, count(DISTINCT a) AS n ORDER BY n DESC LIMIT 5"},
    {"id": "M03", "tier": "medium", "ordered": False,
     "question": "How many distinct admissions included an ICU stay?",
     "cypher": "MATCH (a:Admission)-[:HAS_ICU_STAY]->(:ICUStay) "
               "RETURN count(DISTINCT a) AS n"},
    {"id": "M04", "tier": "medium", "ordered": False,
     "question": "Of all DKA admissions, how many included an ICU stay?",
     "cypher": "MATCH (a:Admission {is_dka: true})-[:HAS_ICU_STAY]->(:ICUStay) "
               "RETURN count(DISTINCT a) AS with_icu"},
    {"id": "M05", "tier": "medium", "ordered": True,
     "question": "What are the five most common principal diagnoses across "
                 "all admissions, most frequent first?",
     "cypher": "MATCH (a:Admission)-[h:HAS_DIAGNOSIS]->(d:Diagnosis) "
               "WHERE h.seq_num = 1 "
               "RETURN d.long_title AS diagnosis, count(*) AS n "
               "ORDER BY n DESC LIMIT 5"},
    {"id": "M06", "tier": "medium", "ordered": False,
     "question": "How many distinct lab measurement types are recorded, "
                 "counting the same label in a different body fluid as "
                 "a different type?",
     "cypher": "MATCH (l:LabEvent) RETURN count(DISTINCT [l.label, l.fluid]) AS n"},
    {"id": "M07", "tier": "medium", "ordered": False,
     "question": "What is the average ICU length of stay, across all ICU "
                 "stays?",
     "cypher": "MATCH (s:ICUStay) RETURN avg(s.los) AS mean_los"},
    {"id": "M08", "tier": "medium", "ordered": False,
     "question": "How many DKA admissions have no panel lab recorded at all?",
     "cypher": "MATCH (a:Admission {is_dka: true}) "
               "WHERE NOT (a)-[:HAS_LAB_EVENT]->() RETURN count(a) AS n"},

    # ---- hard ------------------------------------------------------------
    {"id": "H01", "tier": "hard", "ordered": False,
     "question": "Which single patient has the most DKA admissions, and "
                 "how many do they have?",
     "cypher": "MATCH (p:Patient)-[:HAS_ADMISSION]->(a:Admission {is_dka: true}) "
               "WITH p, count(a) AS n "
               "RETURN p.subject_id AS subject_id, n ORDER BY n DESC LIMIT 1"},
    {"id": "H02", "tier": "hard", "ordered": False, "dynamic": "top_dka_patient",
     "question": "For patient {subject_id}, how many admissions do they have "
                 "in total, and how many of those are DKA admissions?",
     "cypher": "MATCH (p:Patient {{subject_id: {subject_id}}})"
               "-[:HAS_ADMISSION]->(a:Admission) "
               "RETURN count(a) AS total, "
               "count(CASE WHEN a.is_dka THEN 1 END) AS dka"},
    {"id": "H03", "tier": "hard", "ordered": False,
     "question": "Which admission recorded the highest Blood glucose value, "
                 "and what was it?",
     "cypher": "MATCH (a:Admission)-[:HAS_LAB_EVENT]->"
               "(l:LabEvent {label: 'Glucose', fluid: 'Blood'}) "
               "WHERE l.valuenum IS NOT NULL "
               "RETURN a.hadm_id AS hadm_id, l.valuenum AS value "
               "ORDER BY l.valuenum DESC LIMIT 1"},
    {"id": "H04", "tier": "hard", "ordered": True,
     "question": "Comparing DKA admissions to non-DKA admissions, what is "
                 "the average ICU length of stay for each, among admissions "
                 "that had an ICU stay? Show DKA first.",
     "cypher": "MATCH (a:Admission)-[:HAS_ICU_STAY]->(s:ICUStay) "
               "RETURN a.is_dka AS is_dka, avg(s.los) AS mean_los, "
               "count(s) AS n ORDER BY is_dka DESC"},
    {"id": "H05", "tier": "hard", "ordered": False,
     "question": "How many DKA admissions had no procedures recorded at all?",
     "cypher": "MATCH (a:Admission {is_dka: true}) "
               "WHERE NOT (a)-[:HAS_PROCEDURE]->() RETURN count(a) AS n"},
    {"id": "H06", "tier": "hard", "ordered": False,
     "question": "What is the most common first ICU care unit specifically "
                 "for DKA admissions, and how many times does it occur?",
     "cypher": "MATCH (a:Admission {is_dka: true})-[:HAS_ICU_STAY]->(s:ICUStay) "
               "RETURN s.first_careunit AS unit, count(*) AS n "
               "ORDER BY n DESC LIMIT 1"},
    {"id": "H07", "tier": "hard", "ordered": True, "dynamic": "top_dka_patient",
     "question": "For patient {subject_id}, list their DKA admissions in "
                 "chronological order by admission date.",
     "cypher": "MATCH (p:Patient {{subject_id: {subject_id}}})"
               "-[:HAS_ADMISSION]->(a:Admission {{is_dka: true}}) "
               "RETURN a.hadm_id AS hadm_id, a.admittime AS admittime "
               "ORDER BY a.admittime"},
    {"id": "H08", "tier": "hard", "ordered": False,
     "question": "What is the average length, in days, of a DKA admission "
                 "from admission to discharge, rounded to 1 decimal place?",
     "cypher": "MATCH (a:Admission {is_dka: true}) "
               "RETURN round(avg(duration.inSeconds(a.admittime, a.dischtime).seconds "
               "/ 86400.0), 1) AS mean_days"},

    # ---- dka-specific ------------------------------------------------------
    {"id": "D01", "tier": "dka", "ordered": False,
     "question": "Among patients with two or more DKA admissions, what is "
                 "the average interval, in days, between one DKA admission "
                 "and the next (admission time to admission time), rounded to 1 decimal "
                 "place? Also report the number of intervals.",
     "cypher": "MATCH (p:Patient)-[:HAS_ADMISSION]->(a:Admission {is_dka: true}) "
               "WITH p, a ORDER BY a.admittime "
               "WITH p, collect(a.admittime) AS times "
               "WHERE size(times) >= 2 "
               "WITH [i IN range(0, size(times) - 2) | "
               "duration.inSeconds(times[i], times[i + 1]).seconds / 86400.0] AS gaps "
               "UNWIND gaps AS gap "
               "RETURN round(avg(gap), 1) AS mean_gap_days, count(gap) AS n_intervals"},
    {"id": "D02", "tier": "dka", "ordered": False,
     "question": "What is the highest anion gap value recorded during any "
                 "DKA admission, and which admission was it?",
     "cypher": "MATCH (a:Admission {is_dka: true})-[:HAS_LAB_EVENT]->"
               "(l:LabEvent {label: 'Anion Gap'}) "
               "RETURN a.hadm_id AS hadm_id, l.valuenum AS value "
               "ORDER BY l.valuenum DESC LIMIT 1"},
    {"id": "D03", "tier": "dka", "ordered": False,
     "question": "For DKA admissions with at least two bicarbonate "
                 "measurements, what is the average change from the first "
                 "to the last recorded value (last minus first, rounded to 2 decimal "
                 "places)? Also report the number of admissions.",
     "cypher": "MATCH (a:Admission {is_dka: true})-[:HAS_LAB_EVENT]->"
               "(l:LabEvent {label: 'Bicarbonate'}) WHERE l.valuenum IS NOT NULL "
               "WITH a, l ORDER BY l.charttime "
               "WITH a, collect(l.valuenum) AS vals "
               "WHERE size(vals) >= 2 "
               "RETURN round(avg(vals[-1] - vals[0]), 2) AS mean_rise, "
               "count(a) AS n_admissions"},
    {"id": "D04", "tier": "dka", "ordered": False,
     "question": "How many patients had at least one non-DKA admission "
                 "occurring between two of their DKA admissions?",
     "cypher": "MATCH (p:Patient)-[:HAS_ADMISSION]->(a:Admission) "
               "WITH p, a ORDER BY a.admittime "
               "WITH p, collect({is_dka: a.is_dka, t: a.admittime}) AS seq "
               "WITH p, seq, [x IN seq WHERE x.is_dka] AS dkas "
               "WHERE size(dkas) >= 2 "
               "WITH p, seq, dkas[0].t AS first_dka, dkas[-1].t AS last_dka "
               "WITH p, [x IN seq WHERE NOT x.is_dka "
               "AND x.t > first_dka AND x.t < last_dka] AS between "
               "WHERE size(between) > 0 "
               "RETURN count(p) AS n_patients"},
    {"id": "D05", "tier": "dka", "ordered": False,
     "question": "On average, what fraction of a patient's admissions are "
                 "DKA admissions? Compute the fraction per patient, then "
                 "average across patients.",
     "cypher": "MATCH (p:Patient)-[:HAS_ADMISSION]->(a:Admission) "
               "WITH p, count(a) AS total, "
               "count(CASE WHEN a.is_dka THEN 1 END) AS dka "
               "RETURN avg(toFloat(dka) / total) AS mean_fraction"},
    {"id": "D06", "tier": "dka", "ordered": False,
     "question": "Which DKA readmission happened soonest after the patient's "
                 "previous DKA admission, and how many days was the gap (from the previous "
                 "admission's discharge to the readmission's admission, rounded to "
                 "1 decimal place)? Return the readmission's hadm_id and the gap.",
     "cypher": "MATCH (p:Patient)-[:HAS_ADMISSION]->(a:Admission {is_dka: true}) "
               "WITH p, a ORDER BY a.admittime "
               "WITH p, collect({hadm_id: a.hadm_id, t: a.admittime, d: a.dischtime}) AS times "
               "WHERE size(times) >= 2 "
               "WITH [i IN range(0, size(times) - 2) | "
               "{hadm_id: times[i + 1].hadm_id, "
               "gap: round(duration.inSeconds(times[i].d, times[i + 1].t).seconds / 86400.0, 1)}] AS gaps "
               "UNWIND gaps AS g "
               "RETURN g.hadm_id AS hadm_id, g.gap AS gap_days "
               "ORDER BY g.gap ASC LIMIT 1"},

    # ---- adversarial --------------------------------------------------
    # Each exploits exactly one thing a bare structural diagram (arm B)
    # cannot say and the prose notes (arm D) spell out.
    {"id": "X01", "tier": "adversarial", "ordered": True,
     "question": "What is the highest glucose value recorded in the cohort, "
                 "broken out by the fluid it was measured in?",
     "cypher": "MATCH (l:LabEvent {label: 'Glucose'}) "
               "RETURN l.fluid AS fluid, max(l.valuenum) AS max_value "
               "ORDER BY fluid"},
    {"id": "X02", "tier": "adversarial", "ordered": False,
     "question": "What percentage of this cohort's admissions are DKA "
                 "admissions, rounded to 1 decimal place?",
     "cypher": "MATCH (a:Admission) "
               "RETURN round(100.0 * count(CASE WHEN a.is_dka THEN 1 END) "
               "/ count(a), 1) AS pct_dka"},
    {"id": "X03", "tier": "adversarial", "ordered": False,
     "dynamic": "mixed_seq_code",
     "question": "How many times does diagnosis code {icd_code} appear as "
                 "an admission's principal diagnosis (first-listed), versus "
                 "as a secondary diagnosis?",
     "cypher": "MATCH (a:Admission)-[h:HAS_DIAGNOSIS]->"
               "(d:Diagnosis {{icd_code: '{icd_code}'}}) "
               "RETURN count(CASE WHEN h.seq_num = 1 THEN 1 END) AS principal, "
               "count(CASE WHEN h.seq_num > 1 THEN 1 END) AS secondary"},
    {"id": "X04", "tier": "adversarial", "ordered": False,
     "question": "Across DKA admissions that have at least one anion gap "
                 "measurement, what is the average of each admission's peak "
                 "(highest) anion gap?",
     "cypher": "MATCH (a:Admission {is_dka: true})-[:HAS_LAB_EVENT]->"
               "(l:LabEvent {label: 'Anion Gap'}) "
               "WITH a, max(l.valuenum) AS peak "
               "RETURN avg(peak) AS mean_peak_gap, "
               "count(a) AS n_admissions_with_gap"},
]


# ===========================================================================
# RESOLVE DYNAMIC QUESTIONS AGAINST THE LIVE GRAPH
# ===========================================================================

def _resolve_top_dka_patient(run):
    r = run("""
        MATCH (p:Patient)-[:HAS_ADMISSION]->(a:Admission {is_dka: true})
        WITH p, count(a) AS n
        RETURN p.subject_id AS subject_id ORDER BY n DESC LIMIT 1
    """)
    return {"subject_id": r[0]["subject_id"]}


def _resolve_mixed_seq_code(run):
    """A diagnosis code that is principal in some admissions, secondary in
    others — the concrete case X03 needs to be a real trap rather than
    hypothetical."""
    r = run("""
        MATCH (a:Admission)-[h:HAS_DIAGNOSIS]->(d:Diagnosis)
        WITH d.icd_code AS code,
             count(CASE WHEN h.seq_num = 1 THEN 1 END) AS principal,
             count(CASE WHEN h.seq_num > 1 THEN 1 END) AS secondary
        WHERE principal > 0 AND secondary > 0
        RETURN code ORDER BY principal + secondary DESC LIMIT 1
    """)
    if not r:
        raise RuntimeError("no diagnosis code is both principal and "
                           "secondary anywhere in the graph — X03 needs "
                           "a different construction")
    return {"icd_code": r[0]["code"]}


_RESOLVERS = {"top_dka_patient": _resolve_top_dka_patient,
             "mixed_seq_code": _resolve_mixed_seq_code}


def build_bank(run, verbose=True):
    """Fill in {subject_id} / {icd_code} placeholders and return a static bank.

    Resolved once, against the live graph. The returned list has every
    question's actual question text and actual Cypher baked in — no further
    lookups happen, including inside verify_gold().
    """
    cache = {}
    bank = []
    for q in _RAW_BANK:
        q = dict(q)
        key = q.pop("dynamic", None)
        if key:
            if key not in cache:
                cache[key] = _RESOLVERS[key](run)
            params = cache[key]
            q["question"] = q["question"].format(**params)
            q["cypher"] = q["cypher"].format(**params)
            q["resolved"] = params
        bank.append(q)

    if verbose:
        by_tier = {}
        for q in bank:
            by_tier.setdefault(q["tier"], 0)
            by_tier[q["tier"]] += 1
        print(f"{len(bank)} questions: " +
              " · ".join(f"{t} {n}" for t, n in by_tier.items()))
        if cache:
            print("resolved: " + ", ".join(f"{k}={v}" for k, v in cache.items()))
    return bank


def save_bank(bank, path="dka_questions.json"):
    json.dump(bank, open(path, "w"), indent=2)
    print(f"saved -> {path}")


def load_bank(path="dka_questions.json"):
    bank = json.load(open(path))
    print(f"{len(bank)} questions")
    return bank


# ===========================================================================
# GOLD — execute the reference queries, no API calls
# ===========================================================================

def verify_gold(bank, run, verbose=True):
    """Run every reference query once. Catches a broken reference query
    before it is ever compared against a model's output — a reference that
    errors or returns nothing is a bug in the bank, not a finding."""
    gold = {}
    bad = []
    for q in bank:
        try:
            rows = run(q["cypher"])
        except Exception as e:
            bad.append((q["id"], f"{type(e).__name__}: {e}"))
            continue
        if not rows:
            bad.append((q["id"], "reference query returned zero rows"))
            continue
        gold[q["id"]] = rows

    if verbose:
        print(f"{len(gold)} / {len(bank)} reference queries verified")
        for qid, msg in bad:
            print(f"  {qid}: {msg}")
    return gold


# ===========================================================================
# SCORING — ported from evaluation.py, independent copy on purpose
# ===========================================================================

def _cell(v):
    if isinstance(v, float):
        return "NaN" if v != v else round(v, 3)
    if isinstance(v, list):
        return tuple(sorted(str(x) for x in v))
    return str(v)


def compare_strict(gold_rows, got_rows, ordered):
    if not got_rows:
        return False
    gcols = list(gold_rows[0].keys())
    if set(got_rows[0].keys()) != set(gcols):
        return False
    gold_t = [tuple(_cell(r[c]) for c in gcols) for r in gold_rows]
    got_t = [tuple(_cell(r[c]) for c in gcols) for r in got_rows]
    key = (lambda xs: xs) if ordered else sorted
    return key(gold_t) == key(got_t)


def _projections(gold_rows, got_rows, ordered):
    if not got_rows:
        return False
    gcols, tcols = list(gold_rows[0].keys()), list(got_rows[0].keys())
    if len(tcols) < len(gcols):
        return False
    gold_t = [tuple(_cell(r[c]) for c in gcols) for r in gold_rows]
    key = (lambda xs: xs) if ordered else sorted
    gold_k = key(gold_t)
    for pick in permutations(tcols, len(gcols)):
        got_t = [tuple(_cell(r[c]) for c in pick) for r in got_rows]
        if key(got_t) == gold_k:
            return True
    return False


def compare(gold_rows, got_rows, ordered):
    """Returns (strict: bool, projection: bool)."""
    s = compare_strict(gold_rows, got_rows, ordered)
    return s, (s or _projections(gold_rows, got_rows, ordered))


# ===========================================================================
# READ-ONLY GUARD + GENERATION
# ===========================================================================

_WRITE = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|CALL\s+db\.create|"
    r"CALL\s+apoc\.(?!meta|schema)|LOAD\s+CSV)\b", re.IGNORECASE)


def _is_read_only(cypher):
    return not _WRITE.search(cypher)


TEXT2CYPHER = """You translate a clinical question into a single read-only \
Cypher query against a Neo4j graph. Use only the labels, relationship types \
and properties given in the schema below — never invent one.

Schema:
{schema}

Question: {question}

Reply with the Cypher query alone. No explanation, no markdown fences."""


_CYPHER_START = re.compile(
    r"^(MATCH|OPTIONAL MATCH|WITH|UNWIND|CALL|RETURN)\b", re.IGNORECASE | re.MULTILINE)


def generate_cypher(schema, question, max_tokens=2000):
    """Requires llm() from the pipeline cell to already be in scope.

    Despite the prompt asking for Cypher alone, a model sometimes opens with
    a sentence of reasoning before the query — the adversarial tier is where
    this showed up, unsurprisingly, since those questions are the ones that
    reward thinking before writing. Trimming to the first Cypher clause
    keyword handles that case; the code-fence strip handles the other common
    one. If neither applies, the text passes through unchanged and a bad
    generation surfaces as a Neo4j syntax error, which is still informative.
    """
    text = llm(TEXT2CYPHER.format(schema=schema, question=question), max_tokens)
    text = re.sub(r"^```(?:cypher)?\s*|\s*```$", "", text.strip(),
                  flags=re.IGNORECASE | re.MULTILINE).strip()
    m = _CYPHER_START.search(text)
    if m and m.start() > 0:
        text = text[m.start():].strip()
    return text


# ===========================================================================
# RUN ONE ARM
# ===========================================================================

def run_arm(schema, label, bank, run, gold=None, verbose=True):
    """Generate Cypher for every question under one schema string, run it,
    score it against gold. Returns a DataFrame, one row per question."""
    if gold is None:
        gold = verify_gold(bank, run, verbose=False)

    rows = []
    for q in bank:
        if q["id"] not in gold:
            continue
        entry = {"id": q["id"], "tier": q["tier"], "arm": label}
        try:
            cypher = generate_cypher(schema, q["question"])
            entry["cypher"] = cypher
            if not _is_read_only(cypher):
                entry.update(strict=False, projection=False, error="write query")
            else:
                got = run(cypher)
                strict, proj = compare(gold[q["id"]], got, q["ordered"])
                entry.update(strict=strict, projection=proj, error=None,
                             n_got=len(got))
        except Exception as e:
            entry.update(strict=False, projection=False,
                         error=f"{type(e).__name__}: {e}")
        rows.append(entry)

    df = pd.DataFrame(rows)
    if verbose:
        report(df, label)
    return df


def run_all_arms(schemas, bank, run, n_runs=1):
    """schemas: {"A hand-written": SCHEMA, "B arrows": SCHEMA_B, ...}.

    n_runs > 1 repeats each arm, which is what a single-run number cannot
    tell you — the demo-graph evaluation moved tiers by double digits across
    identical-temperature reruns, and nothing here is exempt from that.
    """
    gold = verify_gold(bank, run)
    out = {}
    for label, schema in schemas.items():
        runs = [run_arm(schema, f"{label} (run {i+1})", bank, run, gold,
                        verbose=False)
               for i in range(n_runs)]
        out[label] = pd.concat(runs, ignore_index=True)
        report(out[label], label)
    return out


# ===========================================================================
# REPORT
# ===========================================================================

def report(df, label=""):
    print("=" * 58)
    print(f"DKA SCHEMA ARM{'  ·  ' + label if label else ''}")
    print("=" * 58)
    for tier in ("simple", "medium", "hard", "dka", "adversarial"):
        sub = df[df.tier == tier]
        if not len(sub):
            continue
        print(f"  {tier:<12}{len(sub):>3}   "
              f"strict {sub.strict.mean()*100:>5.1f}%   "
              f"projection {sub.projection.mean()*100:>5.1f}%")
    print(f"  {'overall':<12}{len(df):>3}   "
          f"strict {df.strict.mean()*100:>5.1f}%   "
          f"projection {df.projection.mean()*100:>5.1f}%")
    errs = df[df.error.notna()]
    if len(errs):
        print(f"\n  {len(errs)} errors:")
        for r in errs.itertuples():
            print(f"    {r.id}  {r.error}")


def compare_arms(results):
    """results: {label: DataFrame}. Side-by-side strict/projection per tier."""
    tiers = ("simple", "medium", "hard", "dka", "adversarial", "overall")
    print("=" * 72)
    print(f"  {'tier':<12}" + "".join(f"{l:>20}" for l in results))
    print("=" * 72)
    for tier in tiers:
        cells = []
        for label, df in results.items():
            sub = df if tier == "overall" else df[df.tier == tier]
            cells.append(f"{sub.strict.mean()*100:>5.1f} / "
                         f"{sub.projection.mean()*100:>5.1f}%" if len(sub)
                         else "     n/a")
        print(f"  {tier:<12}" + "".join(f"{c:>20}" for c in cells))
    print("\n  cells read strict / projection")


def failures(df, bank, gold, n=10):
    """Questions where the strict criterion failed. Read these before
    concluding one arm beat another — a 5-point gap can be one unstable
    question, not a real effect, exactly as it was on the demo graph."""
    by = {q["id"]: q for q in bank}
    bad = df[~df.strict]
    print(f"{len(bad)} / {len(df)} failed strict\n")
    for r in bad.head(n).itertuples():
        q = by[r.id]
        print("=" * 72)
        print(f"{r.id}  [{r.tier}]  arm={r.arm}")
        print(f"Q: {q['question']}")
        print(f"\nreference:\n  {q['cypher']}")
        print(f"gold: {gold[r.id]}")
        print(f"\ngenerated:\n  {getattr(r, 'cypher', '(none)')}")
        if r.error:
            print(f"error: {r.error}")


# ---------------------------------------------------------------------------
# Usage, after dka_build.py and arrows.py have been exec'd and `run` and
# SCHEMA / SCHEMA_B / SCHEMA_D exist:
#
#     exec(open('dka_questions.py').read())
#     bank = build_bank(run)
#     save_bank(bank, '/content/drive/MyDrive/graphrag_clinical/dka/questions.json')
#     gold = verify_gold(bank, run)
#
#     results = run_all_arms(
#         {"A hand-written": SCHEMA, "B arrows": SCHEMA_B, "D arrows+notes": SCHEMA_D},
#         bank, run, n_runs=3)
#
#     compare_arms(results)
#     failures(results["B arrows"], bank, gold)
# ---------------------------------------------------------------------------
