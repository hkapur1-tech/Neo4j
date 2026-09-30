"""
experiment.py — routing versus context concatenation.

THE CLAIM UNDER TEST
  Routing a question to one retrieval path beats running both paths and
  concatenating their contexts, because concatenation dilutes an exact
  structured answer with approximate passages whenever the question was
  structural to begin with.

  Arm R (routing)       question -> router -> Path A only -> answer
  Arm C (concatenation) question -> BOTH paths always -> merged context -> answer

  Both arms use the SAME synthesis prompt and the same model. The only
  difference is what goes in the context window, so the measurement isolates
  context composition, not retrieval quality.

SCORING
  Structural questions have reference row sets (from evaluation.py), so the
  answer can be scored automatically: every reference value must appear in
  the answer text. No human annotation, no clinical judgement.

  Only questions whose reference answer holds at most MAX_VALS values are
  scored. A question returning 52 medications cannot be judged by answer-level
  containment — any readable answer summarises rather than lists, and it would
  fail in both arms equally. Those are reported as excluded, not as failures.

REQUIRES
  From the pipeline cell : cypher, client, llm, text2cypher, validate, MODEL
  From evaluation.py     : QUESTIONS
  From the Path B cell   : model (SentenceTransformer), chunks, emb, search
"""

import json, re, time
import numpy as np

MAX_VALS = 6      # answer-level scoring applies below this reference size
TOP_K    = 5      # passages retrieved per question


# ===========================================================================
# SHARED SYNTHESIS — identical in both arms
# ===========================================================================

SYNTH = """Answer the clinical question using only the context below.

Question: {q}

Context:
{ctx}

Answer in two or three sentences. State values exactly as they appear in the
context. If the context does not contain the answer, say so plainly rather
than inferring. Where the answer involves a sequence of events over time,
describe the association as temporal, not causal."""


def synthesize(question, context):
    return llm(SYNTH.format(q=question, ctx=context), 700)


# ===========================================================================
# PATH A and PATH B as context producers
# ===========================================================================

def path_a(question, sid=None):
    """Text2Cypher -> validate -> execute. Returns (context, cypher, rows)."""
    q = text2cypher(question, sid)
    ok, q2 = validate(q)
    if not ok:
        return "", q, []
    try:
        rows = cypher(q2)
    except Exception as e:
        return f"[graph query failed: {type(e).__name__}]", q2, []
    if not rows:
        return "[the graph returned no rows for this question]", q2, []
    return ("Structured records from the clinical graph:\n"
            + json.dumps(rows[:50], indent=1, default=str)), q2, rows


def path_b(question, hadm_id=None, subject_id=None, k=TOP_K):
    """Dense retrieval over discharge summaries. Returns (context, hits)."""
    hits = search(question, k=k, hadm_id=hadm_id, subject_id=subject_id)
    if len(hits) == 0:
        return "[no clinical notes matched this question]", hits
    parts = [f"[note {r.note_id if hasattr(r,'note_id') else ''} "
             f"admission {r.hadm_id}]\n{r.text}" for r in hits.itertuples()]
    return "Passages from clinical notes:\n\n" + "\n\n".join(parts), hits


# ===========================================================================
# ARM R — routing
# ===========================================================================

ROUTE3 = """You route clinical questions to a retrieval path.

STRUCTURED — answerable from recorded fields: admissions, diagnoses,
  procedures, medication records, lab values. What, when, how many, in order.
NARRATIVE — requires clinical reasoning or justification recorded only in
  free-text notes. Why, what was considered, what was ruled out.
COMPOSITE — needs a recorded fact to locate an admission, AND reasoning from
  that admission's notes. E.g. "why was therapy changed after the admission
  where creatinine peaked?"

Question: {q}

Reply with exactly one word: STRUCTURED, NARRATIVE or COMPOSITE."""


def route3(question):
    out = llm(ROUTE3.format(q=question), 200).upper()
    for label in ("COMPOSITE", "NARRATIVE", "STRUCTURED"):
        if label in out:
            return label
    return "STRUCTURED"


def arm_routing(question, sid=None):
    """One path, chosen before any query is generated."""
    path = route3(question)

    if path == "STRUCTURED":
        ctx, cy, rows = path_a(question, sid)
        return {"arm": "routing", "route": path, "cypher": cy,
                "rows": len(rows), "passages": 0,
                "answer": synthesize(question, ctx)}

    if path == "NARRATIVE":
        ctx, hits = path_b(question, subject_id=sid)
        return {"arm": "routing", "route": path, "cypher": None,
                "rows": 0, "passages": len(hits),
                "answer": synthesize(question, ctx)}

    # COMPOSITE — Path A narrows the search space, then Path B runs inside it
    ctx_a, cy, rows = path_a(question, sid)
    hadm = next((r["hadm_id"] for r in rows if "hadm_id" in r), None)
    ctx_b, hits = path_b(question, hadm_id=hadm, subject_id=None if hadm else sid)
    return {"arm": "routing", "route": path, "cypher": cy,
            "rows": len(rows), "passages": len(hits), "scoped_to": hadm,
            "answer": synthesize(question, ctx_a + "\n\n" + ctx_b)}


# ===========================================================================
# ARM C — concatenation (no routing, both paths always)
# ===========================================================================

def arm_concat(question, sid=None):
    """Both paths unconditionally, contexts merged. No routing, no scoping —
    this is the fusion design the comparison is against, implemented
    faithfully rather than as a straw man."""
    ctx_a, cy, rows = path_a(question, sid)
    ctx_b, hits     = path_b(question, subject_id=sid)
    return {"arm": "concat", "route": None, "cypher": cy,
            "rows": len(rows), "passages": len(hits),
            "answer": synthesize(question, ctx_a + "\n\n" + ctx_b)}


# ===========================================================================
# SCORING
# ===========================================================================

def _variants(v):
    """Surface forms a value may legitimately take in prose."""
    s = str(v)
    out = {s, s.lower()}
    # '2150-05-09 16:09:00' -> also accept the date alone
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T]", s)
    if m:
        out.add(m.group(1))
    # 2.2000000001 -> '2.2'
    try:
        f = float(s)
        out.update({f"{f:g}", f"{round(f, 1):g}", str(int(f)) if f == int(f) else s})
    except ValueError:
        pass
    return {x.lower() for x in out if x}


def answer_contains(gold_rows, answer):
    """Every reference value must appear in the answer, in some surface form."""
    a = (answer or "").lower()
    for row in gold_rows:
        for v in row.values():
            if not any(x in a for x in _variants(v)):
                return False, str(v)
    return True, ""


def eligible(questions=None):
    """Questions with a reference answer small enough to score in prose."""
    qs = questions if questions is not None else QUESTIONS
    out = []
    for q in qs:
        if not q.get("gold") or q.get("expect_empty"):
            continue
        try:
            rows = cypher(q["gold"])
        except Exception:
            continue
        n = sum(len(r) for r in rows)
        if 0 < n <= MAX_VALS:
            out.append((q, rows))
    return out


# ===========================================================================
# RUN
# ===========================================================================

def run_experiment(pairs=None, verbose=True):
    """Both arms over the eligible structural questions."""
    pairs = pairs if pairs is not None else eligible()
    SID = 10014354
    results = []

    for q, gold_rows in pairs:
        sid = SID if str(SID) in q["q"] else None
        rec = {"id": q["id"], "tier": q["tier"], "question": q["q"],
               "gold_rows": gold_rows}

        for name, fn in (("R", arm_routing), ("C", arm_concat)):
            t = time.time()
            try:
                out = fn(q["q"], sid)
                ok, missing = answer_contains(gold_rows, out["answer"])
            except Exception as e:
                out = {"answer": f"[ERROR {type(e).__name__}: {e}]"}
                ok, missing = False, "error"
            rec[name] = {**out, "correct": ok, "missing": missing,
                         "secs": round(time.time() - t, 1)}

        results.append(rec)
        if verbose:
            print(f"{rec['id']:<5} routing {'OK ' if rec['R']['correct'] else 'BAD'}"
                  f"  concat {'OK ' if rec['C']['correct'] else 'BAD'}"
                  f"   route={rec['R'].get('route')}")

    return results


def report(results):
    n = len(results)
    r = sum(x["R"]["correct"] for x in results)
    c = sum(x["C"]["correct"] for x in results)

    print("\n" + "=" * 62)
    print("ROUTING vs CONCATENATION — answer-level accuracy")
    print("=" * 62)
    print(f"  n = {n} structural questions "
          f"(reference answer <= {MAX_VALS} values)\n")
    print(f"  routing        {r}/{n}  {100*r/n:>5.1f}%")
    print(f"  concatenation  {c}/{n}  {100*c/n:>5.1f}%")
    print(f"  difference     {100*(r-c)/n:>+5.1f} points")

    # McNemar's counts — the paired test for this design
    both = sum(x["R"]["correct"] and x["C"]["correct"] for x in results)
    ronly = sum(x["R"]["correct"] and not x["C"]["correct"] for x in results)
    conly = sum(x["C"]["correct"] and not x["R"]["correct"] for x in results)
    neither = n - both - ronly - conly
    print(f"\n  paired outcomes:")
    print(f"    both correct        {both}")
    print(f"    routing only        {ronly}")
    print(f"    concatenation only  {conly}")
    print(f"    neither             {neither}")
    print(f"\n  McNemar discordant pairs: b={ronly}, c={conly}")
    if ronly + conly < 10:
        print("  (too few discordant pairs for a reliable test — report counts,"
              "\n   use an exact binomial rather than the chi-square form)")

    bad = [x["id"] for x in results if not x["C"]["correct"] and x["R"]["correct"]]
    if bad:
        print(f"\n  concatenation lost on: {', '.join(bad)}")
    bad2 = [x["id"] for x in results if not x["R"]["correct"] and x["C"]["correct"]]
    if bad2:
        print(f"  routing lost on:       {', '.join(bad2)}")

    print("\n  routes chosen:")
    from collections import Counter
    for k, v in Counter(x["R"].get("route") for x in results).items():
        print(f"    {k:<12}{v}")


def save(results, path="experiment_results.json"):
    with open(path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"saved -> {path}")


def show(results, qid):
    """Print both arms' answers for one question, side by side."""
    x = next(r for r in results if r["id"] == qid)
    print("=" * 72)
    print(x["question"])
    print(f"\nreference: {x['gold_rows']}")
    for arm, label in (("R", "ROUTING"), ("C", "CONCATENATION")):
        a = x[arm]
        print(f"\n--- {label} ({'correct' if a['correct'] else 'wrong'}"
              f"{'' if a['correct'] else ', missing ' + repr(a['missing'])}) ---")
        print(f"route={a.get('route')} rows={a.get('rows')} "
              f"passages={a.get('passages')}")
        print(a["answer"])


# ---------------------------------------------------------------------------
# Usage:
#     pairs = eligible()          # no API cost — just runs reference queries
#     print(f"{len(pairs)} scoreable questions")
#     results = run_experiment(pairs)
#     report(results); save(results)
#     show(results, "M02")        # inspect any single question
#
# For five runs with a range, as in evaluation.py:
#     runs = [run_experiment(pairs, verbose=False) for _ in range(5)]
# ---------------------------------------------------------------------------
