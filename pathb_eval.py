"""
pathb_eval.py — retrieval evaluation for Path B.

THE PROBLEM
  Structural questions have reference row sets, so Text2Cypher can be scored
  automatically. Interpretive questions have no such reference: there is no
  ground truth for "why was therapy changed" short of a clinician writing one.

THE APPROACH
  Build the reference from the notes themselves. Sample a passage, have the
  model write a question whose answer lies in that passage, and keep the
  passage as the gold target. Retrieval is then scored as an information-
  retrieval task — does the top-k passage set contain the one the question was
  written from — rather than as a claim about clinical correctness.

  This is how SQuAD-style benchmarks are built. It is standard, citable, and
  needs no clinician. It is also weaker than human annotation, and the ways it
  is weaker are listed below rather than left for a reviewer to find.

WHAT THIS DOES AND DOES NOT MEASURE
  Measures   : whether dense retrieval surfaces the passage containing the
               answer, at various k, scoped and unscoped.
  Does not   : whether the generated answer is clinically correct, whether the
               passage is the *best* evidence, or whether a clinician would
               have asked that question.

KNOWN BIASES, ALL REPORTED IN THE OUTPUT
  1. Lexical leakage. A generated question that reuses rare terms from its
     source passage is trivially retrievable. The prompt asks for paraphrase,
     and `overlap` reports the measured rare-term overlap so the inflation is
     visible rather than hidden.
  2. Answerability. Questions are generated from a passage, so every question
     is answerable by construction. Real interpretive questions sometimes have
     no answer in the record — the 32 admissions without a discharge summary
     are the obvious case, and this bank contains none of them.
  3. Chunk boundaries are arbitrary. Adjacent chunks overlap by 200 characters
     and the answer may legitimately sit in a neighbour, so both a strict
     (exact chunk) and a same-note score are reported.
  4. The questions are machine-written. Generated pairs should be read and
     bad ones dropped; `review()` prints them for that purpose, and the count
     kept versus generated belongs in the methods.

REQUIRES
  From pathb.py   : chunks, emb, search  (call load() first)
  From the pipeline cell : llm
"""

import json, random, re
import numpy as np
import pandas as pd

SEED = 20260930
BANK = "/content/drive/MyDrive/graphrag_clinical/note/pathb_questions.json"

GEN = """Below is one passage from a hospital discharge summary.

Write ONE question that a clinician might ask about this patient's care, whose
answer is contained in this passage and which asks about reasoning, judgement
or justification — why something was done, what was considered, what was ruled
out, how a decision was reached.

Rules:
  - The answer must be in THIS passage.
  - Paraphrase. Do not reuse distinctive words or phrases from the passage;
    a question that quotes the passage is useless for evaluating retrieval.
  - Do not mention the patient identifier, the admission, or any date.
  - Do not ask a question answerable from structured fields alone (counts,
    dates, drug names, lab values).
  - If the passage contains no clinical reasoning — if it is a medication
    list, a header, a vitals table — reply with exactly: SKIP

Passage:
{passage}

Reply with the question alone, or SKIP."""


# ===========================================================================
# BUILD THE BANK
# ===========================================================================

def generate(n=40, min_chars=400, seed=SEED, path=BANK, verbose=True):
    """Sample passages and write a question from each. Costs n API calls.

    Sampling is stratified by patient so the bank is not dominated by the
    patients with the most admissions.
    """
    rng = random.Random(seed)
    pool = chunks[chunks.text.str.len() >= min_chars]

    by_patient = {}
    for i, r in pool.iterrows():
        by_patient.setdefault(r.subject_id, []).append(i)
    patients = sorted(by_patient)
    rng.shuffle(patients)

    picks, seen = [], set()
    while len(picks) < n * 2 and patients:
        for p in list(patients):
            cand = [i for i in by_patient[p] if i not in seen]
            if not cand:
                patients.remove(p)
                continue
            i = rng.choice(cand)
            seen.add(i)
            picks.append(i)
            if len(picks) >= n * 2:
                break

    bank = []
    for i in picks:
        if len(bank) >= n:
            break
        row = chunks.loc[i]
        try:
            q = llm(GEN.format(passage=row.text), 300).strip()
        except Exception as e:
            if verbose:
                print(f"  chunk {i}: {type(e).__name__}")
            continue
        if q.upper().startswith("SKIP") or len(q) < 20:
            continue
        bank.append({"qid": f"B{len(bank)+1:02d}",
                     "question": q.strip().strip('"'),
                     "gold_chunk": int(i),
                     "gold_note": row.note_id,
                     "hadm_id": int(row.hadm_id),
                     "subject_id": int(row.subject_id),
                     "overlap": _overlap(q, row.text)})
        if verbose:
            print(f"{bank[-1]['qid']}  overlap={bank[-1]['overlap']:.2f}  {q[:80]}")

    json.dump(bank, open(path, "w"), indent=2)
    print(f"\n{len(bank)} questions from {len(picks)} passages sampled "
          f"({len(picks) - len(bank)} skipped or failed)")
    print(f"saved -> {path}")
    return bank


_STOP = set("""the a an and or of to in for with on at by is was were be been are
this that those these it its as from had has have not no than then when which
who whom whose why how what where patient pt he she his her they them their
did does do done being also such into over under after before during while
""".split())


def _overlap(question, passage):
    """Fraction of the question's content words that appear in the passage.

    High values mean the question copied the passage's wording, which makes
    retrieval trivially easy and the score optimistic.
    """
    qw = {w for w in re.findall(r"[a-z]{4,}", question.lower()) if w not in _STOP}
    if not qw:
        return 0.0
    pw = set(re.findall(r"[a-z]{4,}", passage.lower()))
    return len(qw & pw) / len(qw)


def load_bank(path=BANK):
    bank = json.load(open(path))
    print(f"{len(bank)} questions")
    return bank


def review(bank, n=None):
    """Print generated pairs for manual inspection. Drop bad ones by qid."""
    for b in bank[:n]:
        gold = chunks.loc[b["gold_chunk"]]
        print("=" * 72)
        print(f"{b['qid']}  overlap={b['overlap']:.2f}  patient {b['subject_id']}")
        print(f"Q: {b['question']}")
        print(f"\nsource passage:\n{gold.text[:500]}…")


def drop(bank, *qids, path=BANK):
    """Remove questions judged unusable, and save."""
    keep = [b for b in bank if b["qid"] not in set(qids)]
    json.dump(keep, open(path, "w"), indent=2)
    print(f"dropped {len(bank) - len(keep)}, {len(keep)} remain")
    return keep


# ===========================================================================
# SCORE
# ===========================================================================

KS = (1, 3, 5, 10)


def evaluate(bank, ks=KS, scoped=True):
    """Retrieval accuracy. No API calls — retrieval is local.

    `scoped=True` restricts each search to the question's own patient, which
    is what the composite path does: Path A identifies the patient or
    admission, Path B searches inside it. `scoped=False` searches all 3,035
    chunks and is the harder, more honest number.
    """
    kmax = max(ks)
    rows = []
    for b in bank:
        scope = {"subject_id": b["subject_id"]} if scoped else {}
        hits = search(b["question"], k=kmax, **scope)
        idx = list(hits.index)
        notes = list(hits.note_id)

        rank = idx.index(b["gold_chunk"]) + 1 if b["gold_chunk"] in idx else None
        note_rank = (notes.index(b["gold_note"]) + 1
                     if b["gold_note"] in notes else None)
        rows.append({"qid": b["qid"], "overlap": b["overlap"],
                     "rank": rank, "note_rank": note_rank,
                     "pool": int(len(chunks[chunks.subject_id == b["subject_id"]])
                                 if scoped else len(chunks))})
    return pd.DataFrame(rows)


def report(df, ks=KS, label=""):
    n = len(df)
    print("=" * 58)
    print(f"PATH B RETRIEVAL{'  ·  ' + label if label else ''}")
    print("=" * 58)
    print(f"  n = {n} questions · mean pool = {df['pool'].mean():.0f} chunks\n")
    print(f"  {'k':>3}{'exact chunk':>16}{'same note':>14}")
    for k in ks:
        ex = (df["rank"].notna() & (df["rank"] <= k)).mean()
        nt = (df["note_rank"].notna() & (df["note_rank"] <= k)).mean()
        print(f"  {k:>3}{ex*100:>15.1f}%{nt*100:>13.1f}%")

    mrr = df["rank"].apply(lambda r: 0 if pd.isna(r) else 1 / r).mean()
    print(f"\n  MRR (exact chunk)      {mrr:.3f}")
    print(f"  never retrieved        {int(df['rank'].isna().sum())} / {n}")

    hi = df[df["overlap"] >= 0.5]
    lo = df[df["overlap"] < 0.5]
    print(f"\n  lexical overlap with the source passage")
    print(f"    mean                 {df['overlap'].mean():.2f}")
    if len(hi) and len(lo):
        h = (hi["rank"].notna() & (hi["rank"] <= 5)).mean() * 100
        l = (lo["rank"].notna() & (lo["rank"] <= 5)).mean() * 100
        print(f"    high (>=0.5), n={len(hi):<3}   recall@5 {h:.1f}%")
        print(f"    low  (< 0.5), n={len(lo):<3}   recall@5 {l:.1f}%")
        print(f"    difference           {h - l:+.1f} points")
        if h - l > 15:
            print("    -> questions reusing the passage's wording are markedly")
            print("       easier; the headline figure is optimistic by roughly")
            print("       this margin.")


def compare_scopes(bank, ks=KS):
    """Scoped versus unscoped, side by side. Scoping is what routing buys."""
    s = evaluate(bank, ks, scoped=True)
    u = evaluate(bank, ks, scoped=False)
    report(u, ks, "unscoped — all chunks")
    print()
    report(s, ks, "scoped to the patient — what the composite path does")
    print("\n" + "=" * 58)
    print(f"  {'k':>3}{'unscoped':>12}{'scoped':>10}{'gain':>9}")
    for k in ks:
        a = (u["rank"].notna() & (u["rank"] <= k)).mean() * 100
        b = (s["rank"].notna() & (s["rank"] <= k)).mean() * 100
        print(f"  {k:>3}{a:>11.1f}%{b:>9.1f}%{b-a:>+8.1f}")
    print("\n  The gain is the measurable benefit of letting the graph narrow")
    print("  the search space before retrieval runs.")
    return s, u


def failures(df, bank, k=5, n=5):
    """Questions whose gold passage never appeared. Read these."""
    by = {b["qid"]: b for b in bank}
    bad = df[df["rank"].isna() | (df["rank"] > k)]
    print(f"{len(bad)} of {len(df)} missed at k={k}\n")
    for r in bad.head(n).itertuples():
        b = by[r.qid]
        print("=" * 72)
        print(f"{b['qid']}  overlap={b['overlap']:.2f}  rank={r.rank}")
        print(f"Q: {b['question']}")
        print(f"\nwanted:\n{chunks.loc[b['gold_chunk']].text[:300]}…")
        got = search(b["question"], k=1, subject_id=b["subject_id"])
        if len(got):
            print(f"\ngot instead [{got.iloc[0].score:.3f}]:\n{got.iloc[0].text[:300]}…")


# ---------------------------------------------------------------------------
# Usage, after pathb.load():
#
#     exec(open('pathb_eval.py').read())
#     bank = generate(40)          # 40 API calls, the only cost
#     review(bank, 5)              # read a few; drop the bad ones
#     bank = drop(bank, "B07", "B19")
#     s, u = compare_scopes(bank)  # free — retrieval is local
#     failures(s, bank)
#
# Later, without regenerating:
#     bank = load_bank()
# ---------------------------------------------------------------------------
