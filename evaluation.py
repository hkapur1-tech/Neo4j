"""
evaluation.py — question bank and scoring harness (v2).

Measures, against the demo graph, with no credentialed data:

  1. Router accuracy      — does the classifier send each question to the right path?
  2. Text2Cypher accuracy — is the reference answer recoverable from the generated
                            query's result, under two scoring criteria?
  3. Failure modes        — what breaks, and how.

WHAT CHANGED IN v2
  - S12 and M10 rewritten. Both were ambiguous in v1: "most frequently recorded"
    did not say whether to count relationships or distinct admissions, and
    "ranked by number of admissions" did not say whether that meant the patient's
    total admissions or only those carrying the diagnosis. The model chose
    defensible readings and was scored wrong for it. The questions now state the
    grain explicitly.
  - A "hard" tier added: negation, absence, set difference, ordering, duration
    arithmetic and cross-admission comparison. v1's medium tier scored near
    ceiling under projection scoring, so it no longer discriminates. These
    target the failure surface M05 exposed.
  - Adversarial router questions (A01-A04) whose surface form points the wrong
    way: narrative-sounding questions that are structurally answerable, and
    structural-sounding questions that are not.
  - Both scoring criteria reported side by side, and row order is now checked
    on questions that ask for one.

SCORING
  strict     - the returned value multiset must match the reference exactly.
               Extra columns fail.
  projection - the reference answer must be recoverable from the result.
               Extra columns pass.

  Both are defensible and the literature does not consistently state which is
  used, so both are reported. The difference is not cosmetic: in v1 the medium
  tier scored 25.0% strict and 83.3% projection over identical outputs.

  Caveat: an extra column is not always inert. In an aggregating query it joins
  the grouping key and can change the answer - v1's S12 returned different
  fifth-place rows because adding d.icd_code split ICD-9 and ICD-10 codings of
  the same diagnosis title. Projection scoring correctly fails that case.

Requires `driver`, `DB`, `route` and `text2cypher` from the pipeline cell.
Run verify_gold() before evaluate(): it executes every reference query and
reports row counts and errors, without spending any API calls.
"""

import json, time
from itertools import permutations

SID = 10014354          # the longitudinal patient used throughout

# ===========================================================================
# QUESTION BANK
#
#   tier         : simple | medium | hard | narrative | adversarial
#   route        : the path the router should choose
#   gold         : Cypher whose result set is the reference. None for narrative.
#   ordered      : row order is part of the answer (default False)
#   expect_empty : the correct answer is no rows
# ===========================================================================

QUESTIONS = [

# ---------------------------------------------------------------- simple --
{"id":"S01","tier":"simple","route":"STRUCTURED",
 "q":"How many patients are in the database?",
 "gold":"MATCH (p:Patient) RETURN count(p) AS n"},

{"id":"S02","tier":"simple","route":"STRUCTURED",
 "q":"How many hospital admissions are recorded in total?",
 "gold":"MATCH (a:Admission) RETURN count(a) AS n"},

{"id":"S03","tier":"simple","route":"STRUCTURED",
 "q":"How many distinct medications appear in the database?",
 "gold":"MATCH (m:Medication) RETURN count(m) AS n"},

{"id":"S04","tier":"simple","route":"STRUCTURED",
 "q":f"How many admissions has patient {SID} had?",
 "gold":f"MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a) RETURN count(a) AS n"},

{"id":"S05","tier":"simple","route":"STRUCTURED",
 "q":f"What is the gender and anchor age of patient {SID}?",
 "gold":f"MATCH (p:Patient {{subject_id:{SID}}}) RETURN p.gender AS gender, p.anchor_age AS age"},

{"id":"S06","tier":"simple","route":"STRUCTURED",
 "q":f"When was patient {SID} most recently admitted?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
            RETURN max(a.admittime) AS latest"""},

{"id":"S07","tier":"simple","route":"STRUCTURED",
 "q":f"When was patient {SID} first admitted?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
            RETURN min(a.admittime) AS earliest"""},

{"id":"S08","tier":"simple","route":"STRUCTURED",
 "q":"How many distinct diagnosis codes are in the database?",
 "gold":"MATCH (d:Diagnosis) RETURN count(d) AS n"},

{"id":"S09","tier":"simple","route":"STRUCTURED",
 "q":"How many lab events are recorded?",
 "gold":"MATCH (l:LabEvent) RETURN count(l) AS n"},

{"id":"S10","tier":"simple","route":"STRUCTURED",
 "q":"How many procedures are recorded in the database?",
 "gold":"MATCH (p:Procedure) RETURN count(p) AS n"},

{"id":"S11","tier":"simple","route":"STRUCTURED",
 "q":f"How many distinct medications did patient {SID} receive in total?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
                  -[:HAS_MEDICATION]->(m:Medication)
            RETURN count(DISTINCT m) AS n"""},

# v1 asked for "the five most frequently recorded diagnoses", which left both
# the counting unit and the grouping grain unstated.
{"id":"S12","tier":"simple","route":"STRUCTURED",
 "q":"Which five diagnosis titles are recorded on the greatest number of "
     "distinct admissions? Group by the diagnosis title, so that ICD-9 and "
     "ICD-10 codings of the same title count together.",
 "gold":"""MATCH (a:Admission)-[:HAS_DIAGNOSIS]->(d:Diagnosis)
           RETURN d.long_title AS diagnosis, count(DISTINCT a) AS admissions
           ORDER BY admissions DESC, diagnosis LIMIT 5"""},

# ---------------------------------------------------------------- medium --
{"id":"M01","tier":"medium","route":"STRUCTURED",
 "q":f"How many of patient {SID}'s admissions record a leukaemia diagnosis?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
                  -[:HAS_DIAGNOSIS]->(d:Diagnosis)
            WHERE toLower(d.long_title) CONTAINS 'leukemia'
            RETURN count(DISTINCT a) AS n"""},

{"id":"M02","tier":"medium","route":"STRUCTURED",
 "q":f"What was the highest creatinine recorded for patient {SID}?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
                  -[:INCLUDES_LAB]->(l:LabEvent)
            WHERE l.label CONTAINS 'Creatinine'
            RETURN round(max(l.valuenum),1) AS peak"""},

{"id":"M03","tier":"medium","route":"STRUCTURED",
 "q":f"Which medications did patient {SID} receive during the admission where "
     f"creatinine was highest?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
                  -[:INCLUDES_LAB]->(l:LabEvent)
            WHERE l.label CONTAINS 'Creatinine'
            WITH a, max(l.valuenum) AS peak ORDER BY peak DESC LIMIT 1
            MATCH (a)-[:HAS_MEDICATION]->(m:Medication)
            RETURN DISTINCT m.name AS medication ORDER BY medication"""},

{"id":"M04","tier":"medium","route":"STRUCTURED",
 "q":f"Which diagnoses recur across more than one of patient {SID}'s admissions?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
                  -[:HAS_DIAGNOSIS]->(d:Diagnosis)
            WITH d, count(DISTINCT a) AS n WHERE n > 1
            RETURN d.long_title AS diagnosis, n ORDER BY n DESC, diagnosis"""},

# Kept verbatim from v1. This is the one question in the bank that the model
# got genuinely wrong: it filtered on CONTAINS 'remission', which also matches
# "not having achieved remission", returning all 20 admissions instead of 2.
{"id":"M05","tier":"medium","route":"STRUCTURED",
 "q":f"On which admission was patient {SID}'s leukaemia recorded as in remission?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
                  -[:HAS_DIAGNOSIS]->(d:Diagnosis)
            WHERE d.long_title CONTAINS 'in remission'
            RETURN a.admittime AS admitted ORDER BY admitted"""},

{"id":"M06","tier":"medium","route":"STRUCTURED",
 "q":"How many patients have had more than three admissions?",
 "gold":"""MATCH (p:Patient)-[:HAS_ADMISSION]->(a)
           WITH p, count(a) AS n WHERE n > 3
           RETURN count(p) AS n"""},

{"id":"M07","tier":"medium","route":"STRUCTURED",
 "q":f"List patient {SID}'s admissions that record both a leukaemia diagnosis "
     f"and at least one creatinine measurement.",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
                  -[:HAS_DIAGNOSIS]->(d:Diagnosis)
            WHERE toLower(d.long_title) CONTAINS 'leukemia'
            MATCH (a)-[:INCLUDES_LAB]->(l:LabEvent)
            WHERE l.label CONTAINS 'Creatinine'
            RETURN DISTINCT a.admittime AS admitted ORDER BY admitted"""},

{"id":"M08","tier":"medium","route":"STRUCTURED","ordered":True,
 "q":f"Show how patient {SID}'s peak creatinine changed across admissions, "
     f"in date order.",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
                  -[:INCLUDES_LAB]->(l:LabEvent)
            WHERE l.label CONTAINS 'Creatinine'
            RETURN a.admittime AS admitted, round(max(l.valuenum),1) AS peak
            ORDER BY admitted"""},

{"id":"M09","tier":"medium","route":"STRUCTURED",
 "q":f"Which drugs appear alongside venetoclax on patient {SID}'s admissions?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
                  -[:HAS_MEDICATION]->(:Medication {{name:'venetoclax'}})
            MATCH (a)-[:HAS_MEDICATION]->(m:Medication)
            WHERE m.name <> 'venetoclax'
            RETURN DISTINCT m.name AS co_prescribed ORDER BY co_prescribed"""},

# v1 asked "ranked by number of admissions", which the model read as the
# patient's total admissions - the more natural reading, and scored wrong
# against a reference that counted only leukaemia-coded admissions.
{"id":"M10","tier":"medium","route":"STRUCTURED",
 "q":"Which patients have at least one leukaemia diagnosis? Rank them by "
     "their total number of admissions, counting every admission the patient "
     "has, not only those carrying the diagnosis.",
 "gold":"""MATCH (p:Patient)-[:HAS_ADMISSION]->()-[:HAS_DIAGNOSIS]->(d:Diagnosis)
           WHERE toLower(d.long_title) CONTAINS 'leukemia'
           WITH DISTINCT p
           MATCH (p)-[:HAS_ADMISSION]->(a)
           RETURN p.subject_id AS patient, count(DISTINCT a) AS admissions
           ORDER BY admissions DESC, patient"""},

{"id":"M11","tier":"medium","route":"STRUCTURED",
 "q":f"What was the principal diagnosis on patient {SID}'s first admission?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
            WITH a ORDER BY a.admittime LIMIT 1
            MATCH (a)-[r:HAS_DIAGNOSIS]->(d:Diagnosis) WHERE r.seq_num = 1
            RETURN d.long_title AS diagnosis"""},

{"id":"M12","tier":"medium","route":"STRUCTURED","expect_empty":True,
 "q":f"Did patient {SID} ever have a sepsis diagnosis?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
                  -[:HAS_DIAGNOSIS]->(d:Diagnosis)
            WHERE toLower(d.long_title) CONTAINS 'sepsis'
            RETURN a.admittime AS admitted, d.long_title AS diagnosis"""},

# ------------------------------------------------------------------ hard --
# Negation, absence, set difference, ordering, duration and cross-admission
# comparison - the surface M05 exposed. Substring matching is the trap in
# H01; the rest require constructs Text2Cypher tends to omit.

{"id":"H01","tier":"hard","route":"STRUCTURED",
 "q":f"On which of patient {SID}'s admissions was leukaemia recorded as "
     f"active - that is, not in remission?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
                  -[:HAS_DIAGNOSIS]->(d:Diagnosis)
            WHERE toLower(d.long_title) CONTAINS 'leukemia'
              AND NOT toLower(d.long_title) CONTAINS 'in remission'
            RETURN DISTINCT a.admittime AS admitted ORDER BY admitted"""},

{"id":"H02","tier":"hard","route":"STRUCTURED",
 "q":f"Which of patient {SID}'s admissions have no abnormal creatinine "
     f"recorded at all?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
            WHERE NOT EXISTS {{
              MATCH (a)-[:INCLUDES_LAB]->(l:LabEvent)
              WHERE l.label CONTAINS 'Creatinine'
            }}
            RETURN a.admittime AS admitted ORDER BY admitted"""},

{"id":"H03","tier":"hard","route":"STRUCTURED",
 "q":f"Which medications were recorded on patient {SID}'s prednisone "
     f"admissions but never on any of their venetoclax admissions?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
                  -[:HAS_MEDICATION]->(:Medication {{name:'prednisone'}})
            MATCH (a)-[:HAS_MEDICATION]->(m:Medication)
            WITH collect(DISTINCT m.name) AS on_pred
            MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(b)
                  -[:HAS_MEDICATION]->(:Medication {{name:'venetoclax'}})
            MATCH (b)-[:HAS_MEDICATION]->(m2:Medication)
            WITH on_pred, collect(DISTINCT m2.name) AS on_ven
            UNWIND [x IN on_pred WHERE NOT x IN on_ven] AS only_prednisone
            RETURN only_prednisone ORDER BY only_prednisone"""},

{"id":"H04","tier":"hard","route":"STRUCTURED","expect_empty":True,
 "q":f"Were prednisone and venetoclax ever recorded on the same admission "
     f"for patient {SID}?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
                  -[:HAS_MEDICATION]->(:Medication {{name:'prednisone'}})
            MATCH (a)-[:HAS_MEDICATION]->(:Medication {{name:'venetoclax'}})
            RETURN DISTINCT a.hadm_id AS hadm_id"""},

{"id":"H05","tier":"hard","route":"STRUCTURED","ordered":True,
 "q":f"List patient {SID}'s five most recent admissions, most recent first, "
     f"with the discharge location for each.",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
            RETURN a.admittime AS admitted, a.discharge_location AS discharged_to
            ORDER BY admitted DESC LIMIT 5"""},

{"id":"H06","tier":"hard","route":"STRUCTURED","ordered":True,
 "q":f"For patient {SID}'s most recent admission, list the diagnoses in the "
     f"order they were coded, principal diagnosis first.",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
            WITH a ORDER BY a.admittime DESC LIMIT 1
            MATCH (a)-[r:HAS_DIAGNOSIS]->(d:Diagnosis)
            RETURN d.long_title AS diagnosis ORDER BY r.seq_num"""},

{"id":"H07","tier":"hard","route":"STRUCTURED",
 "q":"How many distinct diagnosis titles are recorded across the database, "
     "counting ICD-9 and ICD-10 codings of the same title only once?",
 "gold":"MATCH (d:Diagnosis) RETURN count(DISTINCT d.long_title) AS n"},

{"id":"H08","tier":"hard","route":"STRUCTURED",
 "q":f"Between patient {SID}'s first and last admission, how many days "
     f"elapsed?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
            WITH min(a.admittime) AS first, max(a.admittime) AS last
            RETURN duration.inDays(
                     datetime(replace(first,' ','T')),
                     datetime(replace(last,' ','T'))).days AS days"""},

{"id":"H09","tier":"hard","route":"STRUCTURED",
 "q":f"Across patient {SID}'s admissions that record creatinine, which "
     f"admission showed the largest rise in peak creatinine compared with the "
     f"previous such admission?",
 "gold":f"""MATCH (:Patient {{subject_id:{SID}}})-[:HAS_ADMISSION]->(a)
                  -[:INCLUDES_LAB]->(l:LabEvent)
            WHERE l.label CONTAINS 'Creatinine'
            WITH a, max(l.valuenum) AS peak ORDER BY a.admittime
            WITH collect({{t:a.admittime, p:peak}}) AS xs
            UNWIND range(1, size(xs)-1) AS i
            WITH xs[i] AS cur, xs[i-1] AS prev
            RETURN cur.t AS admitted, round(cur.p - prev.p, 1) AS rise
            ORDER BY rise DESC LIMIT 1"""},

{"id":"H10","tier":"hard","route":"STRUCTURED",
 "q":"Which patients have recorded admissions but no medication records on "
     "any of them?",
 "gold":"""MATCH (p:Patient)-[:HAS_ADMISSION]->()
           WITH DISTINCT p
           WHERE NOT EXISTS {
             MATCH (p)-[:HAS_ADMISSION]->()-[:HAS_MEDICATION]->()
           }
           RETURN p.subject_id AS patient ORDER BY patient"""},

# ------------------------------------------------------------- narrative --
# Router test only. No reference Cypher: not answerable from structure.
{"id":"N01","tier":"narrative","route":"NARRATIVE","gold":None,
 "q":"Why was venetoclax started instead of continuing prednisone?"},

{"id":"N02","tier":"narrative","route":"NARRATIVE","gold":None,
 "q":"What did the team think was driving the decline in renal function?"},

{"id":"N03","tier":"narrative","route":"NARRATIVE","gold":None,
 "q":"What alternative diagnoses were considered and ruled out?"},

{"id":"N04","tier":"narrative","route":"NARRATIVE","gold":None,
 "q":"How did the patient tolerate the change in therapy?"},

{"id":"N05","tier":"narrative","route":"NARRATIVE","gold":None,
 "q":"What was the reasoning behind the discharge plan?"},

{"id":"N06","tier":"narrative","route":"NARRATIVE","gold":None,
 "q":"Why was the patient readmitted so soon after the previous discharge?"},

# ----------------------------------------------------------- adversarial --
# Surface form points the wrong way. A01 and A02 read as narrative requests
# but are answerable from recorded fields; A03 and A04 read as enumerations
# but require reasoning found only in notes. Router test only.
{"id":"A01","tier":"adversarial","route":"STRUCTURED","gold":None,
 "q":f"Tell me what happened to patient {SID}'s creatinine over time."},

{"id":"A02","tier":"adversarial","route":"STRUCTURED","gold":None,
 "q":f"Describe the sequence of therapies patient {SID} received."},

{"id":"A03","tier":"adversarial","route":"NARRATIVE","gold":None,
 "q":"List the concerns documented about this patient's renal function."},

{"id":"A04","tier":"adversarial","route":"NARRATIVE","gold":None,
 "q":"How many times did the team consider stopping treatment?"},
]


# ===========================================================================
# SCORING
# ===========================================================================

def run(cypher_text):
    """Execute Cypher, return rows as a list of dicts. Raises on error."""
    with driver.session(database=DB) as s:
        return [r.data() for r in s.run(cypher_text)]


def _cell(v):
    if isinstance(v, float):
        # NaN is never equal to itself, so it needs a sentinel: otherwise a row
        # containing one fails comparison against an identical row. The graph
        # carried NaN properties where pandas wrote them through (now cleaned,
        # but the loaders can reintroduce them).
        return "NaN" if v != v else round(v, 3)
    if isinstance(v, list):
        return tuple(sorted(str(x) for x in v))
    return str(v)


def normalise(rows, ordered=False):
    """Canonical form: column names kept, values type-normalised.

    Row order is preserved when `ordered`, otherwise sorted away.
    """
    out = [tuple(sorted((k, _cell(v)) for k, v in r.items())) for r in rows]
    return out if ordered else sorted(out)


def _valsets(rows):
    """Each row reduced to the set of its values, names discarded."""
    return [frozenset(_cell(v) for v in r.values()) for r in rows]


def _projections(gold_rows, got_rows, ordered):
    """Yield True if some injective map of reference columns onto result
    columns reproduces the reference rows exactly.

    Matching whole columns rather than individual cells: a per-row subset
    test lets a reference value match a coincidentally equal value in an
    unrelated column, which is not evidence the question was answered.
    """
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


def compare_strict(gold_rows, got_rows, ordered=False):
    """Exact value multiset. Extra columns fail."""
    if normalise(gold_rows, ordered) == normalise(got_rows, ordered):
        return "exact"
    gv = [_cell(v) for r in gold_rows for v in r.values()]
    tv = [_cell(v) for r in got_rows for v in r.values()]
    if sorted(map(str, gv)) == sorted(map(str, tv)):
        return "values_match"
    return "wrong"


def compare(gold_rows, got_rows, ordered=False):
    """Correct if the reference answer is recoverable from the result.

    Some subset of the result's columns, renamed, must reproduce the
    reference rows exactly. Extra columns therefore pass, but row
    multiplication from a cartesian product does not. When `ordered`, rows
    must correspond position by position.
    """
    if normalise(gold_rows, ordered) == normalise(got_rows, ordered):
        return "exact", ""

    if len(got_rows) == 0 and len(gold_rows) > 0:
        return "empty", "returned no rows"
    if len(got_rows) != len(gold_rows):
        return "wrong", f"{len(got_rows)} rows, expected {len(gold_rows)}"
    if not gold_rows:
        return "exact", ""

    if _projections(gold_rows, got_rows, ordered):
        extra = len(got_rows[0]) - len(gold_rows[0])
        return "values_match", (f"{extra} extra column(s)" if extra
                                else "column names differ")

    if ordered:
        return "wrong", "same row count, wrong order or different values"
    return "wrong", "same row count, different values"


# ===========================================================================
# GOLD VERIFICATION - run this before evaluate(). Costs no API calls.
# ===========================================================================

def verify_gold(questions=QUESTIONS):
    """Execute every reference query. Reports row counts and errors.

    A reference query that errors, or returns rows when the question expects
    none (or the reverse), is a bug in the bank, not a model failure. Fix
    those before spending API calls on a run.
    """
    bad = []
    for q in questions:
        if not q.get("gold"):
            continue
        try:
            rows = run(q["gold"])
        except Exception as e:
            print(f"{q['id']:<5} ERROR  {type(e).__name__}: {str(e)[:90]}")
            bad.append(q["id"])
            continue

        note = ""
        if q.get("expect_empty") and rows:
            note = "   <-- expected empty, returned rows"
            bad.append(q["id"])
        elif not q.get("expect_empty") and not rows:
            note = "   <-- returned nothing"
            bad.append(q["id"])
        print(f"{q['id']:<5} {len(rows):>4} row(s)   {str(rows[:1])[:66]}{note}")

    print(f"\n{len(bad)} reference quer{'y' if len(bad) == 1 else 'ies'} "
          f"need attention" + (": " + ", ".join(bad) if bad else "."))
    return bad


# ===========================================================================
# RUN
# ===========================================================================

def evaluate(questions=QUESTIONS, verbose=True):
    """Run the full bank. Returns a list of per-question result dicts."""
    results = []

    for item in questions:
        r = {"id": item["id"], "tier": item["tier"],
             "expected_route": item["route"], "question": item["q"]}

        # ---- 1. router -----------------------------------------------------
        t0 = time.time()
        try:
            r["got_route"] = route(item["q"])
        except Exception as e:
            r["got_route"] = f"ERROR: {type(e).__name__}"
        r["route_correct"] = r["got_route"] == item["route"]
        r["route_secs"] = round(time.time() - t0, 2)

        # ---- 2. Text2Cypher, questions with a reference query only ---------
        if item.get("gold"):
            ordered = item.get("ordered", False)
            try:
                gold_rows = run(item["gold"])
            except Exception as e:
                r["verdict"] = r["verdict_strict"] = "gold_failed"
                r["detail"] = f"{type(e).__name__}: {e}"
                results.append(r)
                continue

            t1 = time.time()
            try:
                gen = text2cypher(item["q"], SID if str(SID) in item["q"] else None)
                r["generated"] = gen
                got_rows = run(gen)
                r["verdict"], r["detail"] = compare(gold_rows, got_rows, ordered)
                r["verdict_strict"] = compare_strict(gold_rows, got_rows, ordered)
                r["rows_gold"], r["rows_got"] = len(gold_rows), len(got_rows)
                if item.get("expect_empty") and len(got_rows) == 0:
                    r["verdict"] = r["verdict_strict"] = "exact"
            except Exception as e:
                r["verdict"] = r["verdict_strict"] = "cypher_error"
                r["detail"] = f"{type(e).__name__}: {str(e)[:120]}"
            r["cypher_secs"] = round(time.time() - t1, 2)
        else:
            r["verdict"] = r["verdict_strict"] = "n/a"

        results.append(r)
        if verbose:
            ok = "OK " if r["route_correct"] else "BAD"
            print(f"{r['id']:<5} route {ok} {r['got_route']:<10} "
                  f"cypher {r['verdict_strict']:<13} / {r['verdict']}")

    return results


OK = ("exact", "values_match")


def report(results):
    """Print the tables that go in the paper."""
    print("\n" + "=" * 62)
    print("ROUTER ACCURACY")
    print("=" * 62)
    print(f"{'tier':<14}{'n':>5}{'correct':>10}{'accuracy':>12}")
    for t in ("simple", "medium", "hard", "narrative", "adversarial"):
        rows = [r for r in results if r["tier"] == t]
        if rows:
            c = sum(r["route_correct"] for r in rows)
            print(f"{t:<14}{len(rows):>5}{c:>10}{c / len(rows) * 100:>11.1f}%")
    c = sum(r["route_correct"] for r in results)
    print(f"{'overall':<14}{len(results):>5}{c:>10}{c / len(results) * 100:>11.1f}%")

    print("\n" + "=" * 62)
    print("TEXT2CYPHER ACCURACY, BY SCORING CRITERION")
    print("=" * 62)
    print(f"{'tier':<14}{'n':>5}{'strict':>12}{'projection':>14}")
    for t in ("simple", "medium", "hard"):
        rows = [r for r in results if r["tier"] == t and r["verdict"] != "n/a"]
        if rows:
            s = sum(r["verdict_strict"] in OK for r in rows)
            p = sum(r["verdict"] in OK for r in rows)
            print(f"{t:<14}{len(rows):>5}{s / len(rows) * 100:>11.1f}%"
                  f"{p / len(rows) * 100:>13.1f}%")

    print("\nFailure modes (projection criterion):")
    for v in ("empty", "wrong", "cypher_error", "gold_failed"):
        bad = [r for r in results if r.get("verdict") == v]
        if bad:
            print(f"  {v:<14}{len(bad):>3}   " + ", ".join(r["id"] for r in bad))

    print("\nPasses projection but not strict - extra columns only:")
    diff = [r["id"] for r in results
            if r.get("verdict") in OK and r.get("verdict_strict") not in OK]
    print("  " + (", ".join(diff) if diff else "none"))

    print("\nMediGRAF reports 80.0% simple, 51.6% medium (Cypher-only, n=131).")
    print("Its scoring criterion is not stated, so neither column above is")
    print("directly comparable. Report both, and say so.")


def save(results, path="evaluation_results.json"):
    with open(path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"saved -> {path}")


def failures(results, path="failures.txt"):
    """Write every non-passing case with its reference and generated query."""
    gold = {q["id"]: q["gold"] for q in QUESTIONS if q.get("gold")}
    with open(path, "w") as f:
        for r in results:
            if r.get("verdict") in OK + ("n/a",):
                continue
            f.write("=" * 72 + f"\n{r['id']} - {r['question']}\n")
            f.write(f"strict: {r.get('verdict_strict')}   "
                    f"projection: {r.get('verdict')}   {r.get('detail', '')}\n")
            f.write("\n--- REFERENCE ---\n" + str(gold.get(r["id"])) + "\n")
            f.write("\n--- GENERATED ---\n" + str(r.get("generated")) + "\n")
            try:
                g, t = run(gold[r["id"]]), run(r["generated"])
                f.write(f"\nreference rows ({len(g)}): {g[:3]}\n")
                f.write(f"generated rows ({len(t)}): {t[:3]}\n")
            except Exception as e:
                f.write(f"\nre-run failed: {type(e).__name__}: {e}\n")
    print(f"saved -> {path}")


# ---------------------------------------------------------------------------
# Usage, after the connection and pipeline cells:
#
#     verify_gold()          # no API calls - fix any flagged reference first
#     results = evaluate()
#     report(results)
#     save(results); failures(results)
# ---------------------------------------------------------------------------
