"""
dka_build.py — build the DKA cohort graph from MIMIC-IV v3.1.

REQUIRES CREDENTIALED DATA. MIMIC-IV v3.1 is not the open demo release. This
module needs PhysioNet credentialing and a BigQuery access grant on the
authenticated Google account. Nothing it produces — no row, no count derived
from patient data, no notebook output — may be committed to a public
repository or placed in a shared Drive folder. The code is safe to commit;
its output is not.

THE COHORT
  Patients with three or more hospital admissions whose PRINCIPAL diagnosis
  (seq_num = 1) is diabetic ketoacidosis.

      96 patients · 1,979 admissions · 552 of them DKA

  DKA is 86% principal-diagnosis when it appears, against 48% for heart
  failure and 21% for diabetes generally, which is why it was chosen: the
  admission is about the condition rather than merely co-occurring with it.
  96 patients is 9.8% of all patients with a principal-diagnosis DKA
  admission, and they account for 36% of such admissions.

  The ICD definition covers secondary and drug-induced diabetes with
  ketoacidosis in BOTH coding systems. Covering E09.1x without 249.1x is an
  asymmetry that silently biases the cohort toward the ICD-10 era.

WHAT IS LOADED WHERE
  All 1,979 admissions become nodes, so questions about what happened between
  DKA episodes remain askable. LabEvents are loaded only for the 552 DKA
  admissions. Scaling the lab panel to all 1,979 would be roughly 156,000
  LabEvent nodes, against AuraDB Free's 200,000 cap — no headroom. So lab
  trajectories sit where the acute metabolic event is, and 1,427 admissions
  have no HAS_LAB_EVENT relationship at all. Any query that assumes labs
  exist for every admission is wrong on this graph.

  Point-of-care glucose is deliberately excluded. Hourly fingersticks during
  an insulin drip live in mimiciv_3_1_icu.chartevents, not labevents, and for
  552 admissions that is hundreds of thousands of rows. The ~8.5 bicarbonate
  and anion-gap measurements per admission in labevents are enough to show
  gap closure, which is the clinically interesting trajectory.

BUDGET
  Expect roughly 56,000 nodes and 155,000 relationships — about 28% of the
  node cap and 39% of the relationship cap on AuraDB Free. verify() prints
  the actual figures against both.

USAGE
    exec(open('dka_build.py').read())

    run, drv = connect(URI, USER, getpass("password: "))
    wipe(run, confirm="yes i mean it")      # overwrites whatever is there
    dfs = fetch(project=PROJECT)            # ~2 minutes of BigQuery
    build(dfs, run)                         # ~6 minutes of writes
    verify(run)
    drv.close()
"""

import numpy as np
import pandas as pd

HOSP = "physionet-data.mimiciv_3_1_hosp"
ICU = "physionet-data.mimiciv_3_1_icu"

MIN_DKA_ADMISSIONS = 3

# Principal-diagnosis DKA. Both coding systems, including secondary and
# drug-induced diabetes with ketoacidosis. Verified against the dictionary:
# no alcoholic or starvation ketoacidosis is captured by these patterns.
DKA_PRINCIPAL = """
      d.seq_num = 1
      AND ( (d.icd_version = 9  AND (d.icd_code LIKE '2501%'
                                  OR d.icd_code LIKE '2491%'))
         OR (d.icd_version = 10 AND REGEXP_CONTAINS(d.icd_code,
                                      r'^E(08|09|10|11|13)1[01]')) )
"""

# The DKA panel. Exact labels, except A1c, which MIMIC stores as
# "% Hemoglobin A1c" and which an exact-match list misses.
PANEL = ("glucose", "bicarbonate", "anion gap", "ph", "potassium", "sodium",
         "chloride", "creatinine", "urea nitrogen", "lactate",
         "white blood cells", "calculated bicarbonate, whole blood", "ketone")

_PANEL_SQL = ", ".join(f"'{x}'" for x in PANEL)
LAB_FILTER = (f"(LOWER(li.label) IN ({_PANEL_SQL})"
              f" OR LOWER(li.label) LIKE '%hemoglobin a1c%')")

# Reused by every extract query, so the cohort cannot drift between tables.
COHORT_CTE = f"""
WITH dka_adm AS (
  SELECT d.subject_id, d.hadm_id
  FROM `{HOSP}.diagnoses_icd` d
  WHERE {DKA_PRINCIPAL}
),
cohort AS (
  SELECT subject_id FROM dka_adm
  GROUP BY subject_id
  HAVING COUNT(DISTINCT hadm_id) >= {MIN_DKA_ADMISSIONS}
),
dka_cohort_adm AS (
  SELECT hadm_id FROM dka_adm
  WHERE subject_id IN (SELECT subject_id FROM cohort)
)
"""

EXPECTED = {"Patient": 96, "Admission": 1979, "dka_admissions": 552}

# Relationship counts must equal source row counts exactly. They did not on
# the first build: HAS_DIAGNOSIS and HAS_PROCEDURE were MERGEd on the
# (admission, code) pair, so an admission recording the same code twice — the
# same procedure on three separate days — collapsed into one relationship and
# the last chartdate written won. 84 of 1,662 procedure rows disappeared that
# way. The natural key of both tables is (hadm_id, seq_num), so seq_num now
# sits inside the MERGE pattern rather than being SET after it.
EXPECTED_RELS = {"HAS_DIAGNOSIS": 29_902, "HAS_PROCEDURE": 1_662,
                 "PRESCRIBED": 48_128, "HAS_LAB_EVENT": 44_100,
                 "HAS_ADMISSION": 1_979, "HAS_ICU_STAY": 625}


# ===========================================================================
# CONNECT
# ===========================================================================

def connect(uri, user, password, database=None):
    """Return (run, driver). `run(cypher, **params)` yields a list of dicts.

    Pass the database name if the instance has more than one. On AuraDB the
    default is `neo4j`; the instance id is not the database name.
    """
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(uri, auth=(user, password))
    drv.verify_connectivity()

    def run(cypher, **params):
        with drv.session(database=database) as s:
            return [r.data() for r in s.run(cypher, **params)]

    print(f"connected: {uri}")
    return run, drv


def wipe(run, confirm=None):
    """Delete every node and relationship. Irreversible.

    The demo graph is reproducible from notebook 01 in about eight minutes
    from open data, so overwriting it costs nothing permanent — but it is
    still a delete, so it needs the confirmation string.
    """
    if confirm != "yes i mean it":
        raise RuntimeError('refusing to wipe without confirm="yes i mean it"')
    n = run("MATCH (n) RETURN count(n) AS n")[0]["n"]
    print(f"deleting {n:,} nodes…")
    while True:
        left = run("MATCH (n) WITH n LIMIT 20000 DETACH DELETE n "
                   "RETURN count(n) AS n")[0]["n"]
        if left == 0:
            break
    print("empty")


# ===========================================================================
# EXTRACT
# ===========================================================================

def fetch(project, verbose=True):
    """Pull every table the graph needs from BigQuery. Returns a dict of frames.

    Authenticate first:
        !gcloud auth application-default login --no-launch-browser
        !gcloud auth application-default set-quota-project <project>
    """
    from google.cloud import bigquery
    bq = bigquery.Client(project=project)

    Q = {}

    Q["patients"] = f"""{COHORT_CTE}
    SELECT p.subject_id, p.gender, p.anchor_age
    FROM `{HOSP}.patients` p
    WHERE p.subject_id IN (SELECT subject_id FROM cohort)
    """

    Q["admissions"] = f"""{COHORT_CTE}
    SELECT a.subject_id, a.hadm_id, a.admittime, a.dischtime,
           a.admission_type, a.admission_location, a.discharge_location,
           a.hadm_id IN (SELECT hadm_id FROM dka_cohort_adm) AS is_dka
    FROM `{HOSP}.admissions` a
    WHERE a.subject_id IN (SELECT subject_id FROM cohort)
    ORDER BY a.subject_id, a.admittime
    """

    Q["icustays"] = f"""{COHORT_CTE}
    SELECT i.hadm_id, i.stay_id, i.first_careunit, i.intime, i.outtime, i.los
    FROM `{ICU}.icustays` i
    WHERE i.subject_id IN (SELECT subject_id FROM cohort)
    """

    Q["diagnoses"] = f"""{COHORT_CTE}
    SELECT d.hadm_id, d.seq_num, d.icd_code, d.icd_version, t.long_title
    FROM `{HOSP}.diagnoses_icd` d
    LEFT JOIN `{HOSP}.d_icd_diagnoses` t
           ON t.icd_code = d.icd_code AND t.icd_version = d.icd_version
    WHERE d.subject_id IN (SELECT subject_id FROM cohort)
    """

    Q["procedures"] = f"""{COHORT_CTE}
    SELECT p.hadm_id, p.seq_num, p.chartdate,
           p.icd_code, p.icd_version, t.long_title
    FROM `{HOSP}.procedures_icd` p
    LEFT JOIN `{HOSP}.d_icd_procedures` t
           ON t.icd_code = p.icd_code AND t.icd_version = p.icd_version
    WHERE p.subject_id IN (SELECT subject_id FROM cohort)
    """

    # One relationship per (admission, drug), not per prescription row.
    # Per-row would be two to three times the relationship count for no added
    # answerable question. starttime is the FIRST order for that drug in that
    # admission; n_orders records how many were collapsed, so the loss is
    # visible rather than silent.
    Q["prescriptions"] = f"""{COHORT_CTE}
    SELECT hadm_id, drug,
           MIN(starttime) AS starttime,
           ARRAY_AGG(route IGNORE NULLS ORDER BY starttime
                     LIMIT 1)[SAFE_OFFSET(0)] AS route,
           COUNT(*) AS n_orders
    FROM `{HOSP}.prescriptions`
    WHERE subject_id IN (SELECT subject_id FROM cohort)
      AND drug IS NOT NULL
    GROUP BY hadm_id, drug
    """

    # DKA admissions only, and only within the cohort — dka_adm alone spans
    # 970 patients, so omitting the cohort restriction here loads tens of
    # thousands of lab events from the wrong people.
    Q["labevents"] = f"""{COHORT_CTE}
    SELECT le.labevent_id, le.hadm_id, le.itemid,
           li.label, li.fluid,
           le.charttime, le.valuenum, le.valueuom, le.flag
    FROM `{HOSP}.labevents` le
    JOIN `{HOSP}.d_labitems` li ON li.itemid = le.itemid
    WHERE le.hadm_id IN (SELECT hadm_id FROM dka_cohort_adm)
      AND {LAB_FILTER}
    """

    dfs = {}
    for name, sql in Q.items():
        dfs[name] = bq.query(sql).to_dataframe()
        if verbose:
            print(f"  {name:<14} {len(dfs[name]):>8,} rows")

    if verbose:
        a = dfs["admissions"]
        print(f"\n  {a.subject_id.nunique()} patients · {len(a)} admissions · "
              f"{int(a.is_dka.sum())} DKA")
    return dfs


# ===========================================================================
# TYPE CLEANING
# ===========================================================================

def _py(v):
    """numpy/pandas scalar -> something the Neo4j driver stores correctly.

    The float-NaN case matters more than it looks. pandas NaN passed through
    as a float lands in Neo4j as a float NaN, which is not null: `IS NULL`
    is false for it, and NaN never equals itself, so the property is
    unreachable by any predicate. It has to become None.
    """
    if v is None or v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.to_pydatetime()
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if f != f else f
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def _rows(df):
    return [{k: _py(v) for k, v in rec.items()}
            for rec in df.to_dict("records")]


# ===========================================================================
# LOAD
# ===========================================================================

CONSTRAINTS = [
    "CREATE CONSTRAINT patient_id IF NOT EXISTS "
    "FOR (p:Patient) REQUIRE p.subject_id IS UNIQUE",
    "CREATE CONSTRAINT admission_id IF NOT EXISTS "
    "FOR (a:Admission) REQUIRE a.hadm_id IS UNIQUE",
    "CREATE CONSTRAINT icustay_id IF NOT EXISTS "
    "FOR (s:ICUStay) REQUIRE s.stay_id IS UNIQUE",
    "CREATE CONSTRAINT diagnosis_code IF NOT EXISTS "
    "FOR (d:Diagnosis) REQUIRE d.code IS UNIQUE",
    "CREATE CONSTRAINT procedure_code IF NOT EXISTS "
    "FOR (p:Procedure) REQUIRE p.code IS UNIQUE",
    "CREATE CONSTRAINT medication_drug IF NOT EXISTS "
    "FOR (m:Medication) REQUIRE m.drug IS UNIQUE",
    "CREATE CONSTRAINT labevent_id IF NOT EXISTS "
    "FOR (l:LabEvent) REQUIRE l.labevent_id IS UNIQUE",
]

INDEXES = [
    # is_dka partitions nearly every interesting query.
    "CREATE INDEX admission_is_dka IF NOT EXISTS FOR (a:Admission) ON (a.is_dka)",
    "CREATE INDEX admission_admittime IF NOT EXISTS FOR (a:Admission) ON (a.admittime)",
    # label+fluid is the real identity of a lab; label alone is ambiguous.
    "CREATE INDEX lab_label IF NOT EXISTS FOR (l:LabEvent) ON (l.label)",
    "CREATE INDEX lab_charttime IF NOT EXISTS FOR (l:LabEvent) ON (l.charttime)",
]


def schema(run):
    for c in CONSTRAINTS + INDEXES:
        run(c)
    print(f"{len(CONSTRAINTS)} constraints · {len(INDEXES)} indexes")


# Diagnosis and Procedure are keyed on a synthesised `code` because the real
# key is the (icd_code, icd_version) pair, and composite node-key constraints
# are an Enterprise feature. A single unique property gets the same guarantee
# on AuraDB Free.

CY = {
    "patients": """
        UNWIND $rows AS r
        MERGE (p:Patient {subject_id: r.subject_id})
        SET   p.gender = r.gender, p.anchor_age = r.anchor_age
    """,
    "admissions": """
        UNWIND $rows AS r
        MATCH (p:Patient {subject_id: r.subject_id})
        MERGE (a:Admission {hadm_id: r.hadm_id})
        SET   a.admittime          = r.admittime,
              a.dischtime          = r.dischtime,
              a.admission_type     = r.admission_type,
              a.admission_location = r.admission_location,
              a.discharge_location = r.discharge_location,
              a.is_dka             = r.is_dka
        MERGE (p)-[:HAS_ADMISSION]->(a)
    """,
    "icustays": """
        UNWIND $rows AS r
        MATCH (a:Admission {hadm_id: r.hadm_id})
        MERGE (s:ICUStay {stay_id: r.stay_id})
        SET   s.first_careunit = r.first_careunit,
              s.intime = r.intime, s.outtime = r.outtime, s.los = r.los
        MERGE (a)-[:HAS_ICU_STAY]->(s)
    """,
    "diagnoses": """
        UNWIND $rows AS r
        MATCH (a:Admission {hadm_id: r.hadm_id})
        MERGE (d:Diagnosis {code: r.code})
        SET   d.icd_code = r.icd_code, d.icd_version = r.icd_version,
              d.long_title = r.long_title
        MERGE (a)-[:HAS_DIAGNOSIS {seq_num: r.seq_num}]->(d)
    """,
    "procedures": """
        UNWIND $rows AS r
        MATCH (a:Admission {hadm_id: r.hadm_id})
        MERGE (p:Procedure {code: r.code})
        SET   p.icd_code = r.icd_code, p.icd_version = r.icd_version,
              p.long_title = r.long_title
        MERGE (a)-[e:HAS_PROCEDURE {seq_num: r.seq_num}]->(p)
        SET   e.chartdate = r.chartdate
    """,
    "prescriptions": """
        UNWIND $rows AS r
        MATCH (a:Admission {hadm_id: r.hadm_id})
        MERGE (m:Medication {drug: r.drug})
        MERGE (a)-[e:PRESCRIBED]->(m)
        SET   e.starttime = r.starttime, e.route = r.route,
              e.n_orders = r.n_orders
    """,
    "labevents": """
        UNWIND $rows AS r
        MATCH (a:Admission {hadm_id: r.hadm_id})
        MERGE (l:LabEvent {labevent_id: r.labevent_id})
        SET   l.itemid = r.itemid, l.label = r.label, l.fluid = r.fluid,
              l.charttime = r.charttime, l.valuenum = r.valuenum,
              l.valueuom = r.valueuom, l.flag = r.flag
        MERGE (a)-[:HAS_LAB_EVENT]->(l)
    """,
}

ORDER = ["patients", "admissions", "icustays", "diagnoses",
         "procedures", "prescriptions", "labevents"]


def build(dfs, run, batch=5000, verbose=True):
    """Load every frame in dependency order. MERGE throughout, so rerunnable."""
    import time
    schema(run)

    for name in ORDER:
        df = dfs[name].copy()
        if name in ("diagnoses", "procedures"):
            df["code"] = (df.icd_version.astype(str) + ":"
                          + df.icd_code.astype(str))
        rows = _rows(df)
        t0 = time.time()
        for i in range(0, len(rows), batch):
            run(CY[name], rows=rows[i:i + batch])
        if verbose:
            print(f"  {name:<14} {len(rows):>8,} rows  "
                  f"{time.time() - t0:>6.1f}s")
    print("\nloaded")


# ===========================================================================
# VERIFY
# ===========================================================================

AURA_FREE = (200_000, 400_000)


def verify(run):
    """Counts, budget headroom, and the integrity checks that caught real bugs."""
    print("=" * 62)
    print("DKA COHORT GRAPH")
    print("=" * 62)

    labels = run("""
        MATCH (n) UNWIND labels(n) AS l
        RETURN l AS label, count(*) AS n ORDER BY n DESC
    """)
    for r in labels:
        exp = EXPECTED.get(r["label"])
        mark = "" if exp is None else ("  ok" if r["n"] == exp
                                       else f"  EXPECTED {exp:,}")
        print(f"  {r['label']:<14}{r['n']:>10,}{mark}")

    print()
    rels = run("""
        MATCH ()-[e]->() RETURN type(e) AS type, count(*) AS n ORDER BY n DESC
    """)
    for r in rels:
        exp = EXPECTED_RELS.get(r["type"])
        mark = "" if exp is None else ("  ok" if r["n"] == exp
                                      else f"  EXPECTED {exp:,} — rows collapsed")
        print(f"  {r['type']:<20}{r['n']:>10,}{mark}")

    nodes = sum(r["n"] for r in labels)
    edges = sum(r["n"] for r in rels)
    print(f"\n  nodes {nodes:>10,}   {nodes / AURA_FREE[0] * 100:>5.1f}% of Aura Free cap")
    print(f"  edges {edges:>10,}   {edges / AURA_FREE[1] * 100:>5.1f}% of Aura Free cap")

    dka = run("MATCH (a:Admission {is_dka: true}) RETURN count(a) AS n")[0]["n"]
    print(f"\n  DKA admissions {dka:,}" +
          ("  ok" if dka == EXPECTED["dka_admissions"]
           else f"  EXPECTED {EXPECTED['dka_admissions']:,}"))

    print("\n  integrity")

    nan = run("""
        MATCH (l:LabEvent) WHERE l.valuenum <> l.valuenum
        RETURN count(l) AS n
    """)[0]["n"]
    print(f"    float NaN in LabEvent.valuenum        {nan}"
          + ("  ok" if nan == 0 else "  <- these are unreachable by IS NULL"))

    orphan = run("""
        MATCH (a:Admission) WHERE NOT (:Patient)-[:HAS_ADMISSION]->(a)
        RETURN count(a) AS n
    """)[0]["n"]
    print(f"    admissions with no patient            {orphan}"
          + ("  ok" if orphan == 0 else ""))

    # Non-DKA admissions have no labs by design. DKA admissions without labs
    # are NOT by design — they are admissions where no panel lab was recorded
    # at all, and any per-admission trajectory question silently skips them.
    nolab = run("""
        MATCH (a:Admission) WHERE NOT (a)-[:HAS_LAB_EVENT]->()
        RETURN a.is_dka AS is_dka, count(a) AS n
    """)
    by = {r["is_dka"]: r["n"] for r in nolab}
    print(f"    non-DKA admissions, no labs           {by.get(False, 0):,}"
          f"  expected {EXPECTED['Admission'] - EXPECTED['dka_admissions']:,}"
          " by design")
    print(f"    DKA admissions with NO panel lab      {by.get(True, 0):,}"
          "  <- excluded from any lab-trajectory question")

    amb = run("""
        MATCH (l:LabEvent) WITH l.label AS label, collect(DISTINCT l.fluid) AS f
        WHERE size(f) > 1 RETURN label, f ORDER BY label
    """)
    if amb:
        print("\n    labels spanning more than one fluid — a query that filters"
              "\n    on label alone is wrong for these:")
        for r in amb:
            print(f"      {r['label']:<24}{', '.join(sorted(r['f']))}")

    print("\n  NOTE: these figures derive from credentialed MIMIC-IV v3.1."
          "\n  Do not commit this output.")


# ---------------------------------------------------------------------------
# Schema string for the Text2Cypher prompt. Keep this in sync with the
# loader by hand; the Arrows experiment replaces it with a generated export,
# and that replacement is what gets measured against the question bank.
# ---------------------------------------------------------------------------

SCHEMA = """
(:Patient    {subject_id:int, gender:str, anchor_age:int})
(:Admission  {hadm_id:int, admittime:datetime, dischtime:datetime,
              admission_type:str, admission_location:str,
              discharge_location:str, is_dka:bool})
(:ICUStay    {stay_id:int, first_careunit:str, intime:datetime,
              outtime:datetime, los:float})
(:Diagnosis  {code:str, icd_code:str, icd_version:int, long_title:str})
(:Procedure  {code:str, icd_code:str, icd_version:int, long_title:str})
(:Medication {drug:str})
(:LabEvent   {labevent_id:int, itemid:int, label:str, fluid:str,
              charttime:datetime, valuenum:float, valueuom:str, flag:str})

(:Patient)   -[:HAS_ADMISSION]->  (:Admission)
(:Admission) -[:HAS_ICU_STAY]->   (:ICUStay)
(:Admission) -[:HAS_DIAGNOSIS  {seq_num:int}]-> (:Diagnosis)
(:Admission) -[:HAS_PROCEDURE  {seq_num:int, chartdate:date}]-> (:Procedure)
(:Admission) -[:PRESCRIBED     {starttime:datetime, route:str,
                                n_orders:int}]-> (:Medication)
(:Admission) -[:HAS_LAB_EVENT]->  (:LabEvent)

Cohort: 96 patients with three or more admissions whose principal diagnosis
is diabetic ketoacidosis. 1,979 admissions in total; is_dka marks the 552
that are DKA admissions.

LabEvents exist only for DKA admissions. HAS_DIAGNOSIS.seq_num = 1 is the
principal diagnosis. LabEvent.label is ambiguous without fluid — Glucose is
recorded in both Blood and Urine.
"""
