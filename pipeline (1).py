"""
Graph-backed clinical QA pipeline.

    question → router → Text2Cypher → validator → Neo4j → answer

Routing happens before Cypher is generated, because Text2Cypher does not refuse:
given an unanswerable question it produces plausible Cypher returning
related-but-irrelevant rows, and an answer synthesised from those rows reads as
responsive while being unfounded.

Requires `client` (anthropic.Anthropic) and `cypher(q, **params)` from the
connection cell.
"""

import re, time, textwrap

MODEL   = "claude-opus-5"
LIMIT_N = 50

SCHEMA = """
Nodes and properties:
  (:Patient    {subject_id, gender, anchor_age, anchor_year})
  (:Admission  {hadm_id, admission_type, admittime, dischtime, discharge_location})
  (:Diagnosis  {icd_code, long_title, icd_version})
  (:Procedure  {icd_code, long_title})
  (:Medication {name})                       // lowercase, e.g. 'venetoclax'
  (:LabEvent   {label, value, valuenum, valueuom, flag, charttime})

Relationships:
  (:Patient)-[:HAS_ADMISSION]->(:Admission)
  (:Admission)-[:HAS_DIAGNOSIS  {seq_num}]->(:Diagnosis)
  (:Admission)-[:HAS_PROCEDURE  {chartdate}]->(:Procedure)
  (:Admission)-[:HAS_MEDICATION {route, dose, unit, starttime}]->(:Medication)
  (:Admission)-[:INCLUDES_LAB]->(:LabEvent)

Notes:
  - ONLY labs flagged 'abnormal' are stored. Every LabEvent has flag='abnormal';
    normal values are absent from the graph. Never describe a lab result as a
    complete series, and never infer that a patient had normal values because
    no abnormal ones are recorded.
  - Diagnosis, Procedure and Medication nodes are shared across patients.
  - Lab labels are capitalised ('Creatinine'); medication names are lowercase.
  - Match diagnosis text with: toLower(d.long_title) CONTAINS '...'
  - seq_num on HAS_DIAGNOSIS is the diagnosis ordering within that admission;
    seq_num = 1 is the principal diagnosis.
  - Use DISTINCT when combining several MATCH or OPTIONAL MATCH clauses: they
    produce a cartesian product of rows, so a node can appear on many rows and
    a naive count() counts it once per row. The source data is not duplicated.
  - Prefer aggregation (max, min, count, collect) when the question asks for a
    single value or a total. Return raw rows only when the question asks to
    list or trace individual records.
"""


def llm(prompt, max_tokens=2000):
    """Call the model and return its text.

    temperature=0 goes through extra_body because some SDK versions do not
    expose it as a named argument; the value reaches the API either way, and
    without it evaluation runs are not reproducible.

    Raises on truncation. A completion cut off at max_tokens becomes invalid
    Cypher, which then presents as a model failure rather than a budget one.
    """
    r = client.messages.create(
        model=MODEL, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"temperature": 0})
    text = "".join(b.text for b in r.content
                   if getattr(b, "type", "") == "text").strip()
    if r.stop_reason == "max_tokens":
        raise RuntimeError(f"output truncated at max_tokens={max_tokens}")
    if not text:
        raise RuntimeError(
            f"no text returned — stop_reason={r.stop_reason}, "
            f"blocks={[getattr(b, 'type', '?') for b in r.content]}")
    return text


# ---- 0. router ------------------------------------------------------------
def route(question):
    out = llm(f"""You route clinical questions to one of two retrieval paths.

STRUCTURED — answerable from recorded fields: admissions, diagnoses, procedures,
  medication records, lab values. Questions of what, when, how many, in what order.
NARRATIVE — requires clinical reasoning, justification, or differential thinking
  recorded only in free-text notes. Questions of why, what was considered,
  what was ruled out.

Question: {question}

Reply with exactly one word: STRUCTURED or NARRATIVE.""", 200).upper()
    return "NARRATIVE" if "NARRATIVE" in out else "STRUCTURED"


# ---- 1. text2cypher -------------------------------------------------------
def text2cypher(question, sid=None):
    q = llm(f"""You translate clinical questions into Neo4j Cypher.
{SCHEMA}
Question: {question}
{f"Scope to patient subject_id = {sid}." if sid else ""}
Return ONLY the Cypher query. No explanation, no markdown fences.
Always LIMIT results to at most {LIMIT_N} rows.""")
    return re.sub(r'^```(?:cypher)?|```$', '', q, flags=re.M).strip()


# ---- 2. validator ---------------------------------------------------------
# Generated Cypher is untrusted input. The graph is read-only at query time.
BANNED = re.compile(
    r'\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|LOAD\s+CSV)\b', re.I)


def validate(q):
    if BANNED.search(q):
        return False, "rejected — write operation in generated query"
    if not re.search(r'\bLIMIT\b', q, re.I):
        q = q.rstrip().rstrip(';') + f"\nLIMIT {LIMIT_N};"
    return True, q


# ---- the pipeline ---------------------------------------------------------
def ask(question, sid=None):
    print("QUESTION  " + question + "\n" + "─" * 66)

    path = route(question)
    print(f"[0] Router       → {path}\n")

    if path == "NARRATIVE":
        print("[B] Note retrieval\n"
              "    No structural mapping — no node or property holds clinical\n"
              "    reasoning. Routed to dense retrieval over discharge summaries.\n"
              "    Blocked: MIMIC-IV-Note requires PhysioNet credentialing.")
        return None

    t0 = time.time()
    q  = text2cypher(question, sid)
    print(f"[1] Text2Cypher  ({time.time()-t0:.1f}s)\n")
    print(textwrap.indent(q, "    ") + "\n")

    ok, q2 = validate(q)
    print(f"[2] Validation   {'✓ read-only, LIMIT enforced' if ok else '✗ ' + q2}\n")
    if not ok:
        return None

    t1   = time.time()
    rows = cypher(q2)
    print(f"[3] Neo4j        {len(rows)} row(s) in {time.time()-t1:.2f}s\n")

    if not rows:
        print("[3b] Fallback    Path A returned nothing → would route to note\n"
              "                 retrieval (blocked: MIMIC-IV-Note credentialing)")
        return None

    truncated = len(rows) >= LIMIT_N
    if truncated:
        print(f"[!] TRUNCATED    hit the {LIMIT_N}-row limit — this is a partial\n"
              f"                 result, not a complete one\n")

    print("[4] Answer\n")
    print(textwrap.indent(llm(
        f"Question: {question}\n\nRows from the clinical graph:\n{rows}\n\n"
        + (f"IMPORTANT: these rows were truncated at a {LIMIT_N}-row limit. "
           "Say so explicitly and do not present the list as complete.\n\n"
           if truncated else "")
        + "Answer in two or three sentences using only these rows. "
          "If the answer involves a sequence of events over time, state "
          "associations as temporal rather than causal. Otherwise answer plainly.",
        900), "    "))
    return None
