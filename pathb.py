"""
pathb.py — narrative retrieval over discharge summaries.

Loads a previously built index and defines `search()`. Building the index is a
separate, one-off step (see `build_index` at the bottom, or notebook 02 §4):
embedding 3,035 chunks takes about a minute on a GPU and 35 on a CPU, so it is
done once and the result saved.

    demo_notes.parquet   243 discharge summaries for the 100 demo patients
    chunks.parquet       3,035 passages, 1,200 chars with 200 overlap
    emb.npy              (3035, 384) float32, L2-normalised

Embeddings are computed LOCALLY with sentence-transformers. Credentialed note
text never leaves the execution environment, and none of these three files may
be committed or placed in a shared Drive folder.

Retrieval is exact cosine similarity over a numpy array — at this corpus size a
vector database adds operational weight and no accuracy.

Requires the artifacts above. `cypher` is not needed here; scoping is by
subject_id or hadm_id, which Path A supplies.
"""

import os
import numpy as np
import pandas as pd

NOTE_DIR = "/content/drive/MyDrive/graphrag_clinical/note"
EMBEDDER = "BAAI/bge-small-en-v1.5"

# A general-purpose model. On the worked example it ranks the passage that
# answers the question third, behind two topically adjacent but unresponsive
# paragraphs — a clinical-domain model or a reranking step is the obvious
# improvement, and neither is implemented here.

CHUNK, OVERLAP = 1200, 200

chunks = None
emb = None
model = None


def load(note_dir=NOTE_DIR):
    """Load the saved index. Fast — no embedding is computed."""
    global chunks, emb, model
    import torch
    from sentence_transformers import SentenceTransformer

    chunks = pd.read_parquet(f"{note_dir}/chunks.parquet")
    emb = np.load(f"{note_dir}/emb.npy")
    assert len(chunks) == len(emb), (
        f"index mismatch: {len(chunks)} chunks, {len(emb)} vectors — rebuild")

    model = SentenceTransformer(
        EMBEDDER, device="cuda" if torch.cuda.is_available() else "cpu")

    print(f"{len(chunks)} chunks · {emb.shape[1]}d · "
          f"{chunks.hadm_id.nunique()} admissions · "
          f"{chunks.subject_id.nunique()} patients")
    return chunks, emb


def search(question, k=5, hadm_id=None, subject_id=None):
    """Dense retrieval over the note chunks.

    Pass `hadm_id` or `subject_id` to restrict the search. That scoping is the
    composite path: Path A identifies the admission, Path B then searches only
    that admission's notes rather than the whole corpus.

    Returns an empty frame when the scope matches nothing — which is the
    correct answer for an interpretive question about an observation stay, since
    32 of 275 admissions have no discharge summary.
    """
    if chunks is None:
        raise RuntimeError("call load() first")

    mask = np.ones(len(chunks), bool)
    if hadm_id is not None:
        mask &= (chunks.hadm_id == hadm_id).values
    if subject_id is not None:
        mask &= (chunks.subject_id == subject_id).values
    if not mask.any():
        return chunks.iloc[:0].assign(score=pd.Series(dtype=float))

    q = model.encode([question], normalize_embeddings=True)[0]
    sims = emb[mask] @ q                      # normalised, so dot == cosine
    idx = np.argsort(-sims)[:k]

    out = chunks[mask].iloc[idx].copy()
    out["score"] = sims[idx]
    return out[["score", "subject_id", "hadm_id", "note_id", "text"]]


def context(question, k=5, hadm_id=None, subject_id=None):
    """Retrieved passages formatted for a synthesis prompt."""
    hits = search(question, k, hadm_id, subject_id)
    if len(hits) == 0:
        return "[no clinical notes matched this question]", hits
    parts = [f"[note {r.note_id} · admission {r.hadm_id}]\n{r.text}"
             for r in hits.itertuples()]
    return "Passages from clinical notes:\n\n" + "\n\n".join(parts), hits


def show(question, k=5, **scope):
    """Print retrieved passages with scores. For inspection, not for the pipeline."""
    for r in search(question, k, **scope).itertuples():
        print(f"\n[{r.score:.3f}] admission {r.hadm_id}\n{r.text[:400]}…")


# ===========================================================================
# ONE-OFF: build the index
# ===========================================================================

def build_index(notes_df, note_dir=NOTE_DIR):
    """Chunk, embed and save. Run once; use load() thereafter.

    Set the Colab runtime to a GPU first — about one minute against 35 on CPU.
    """
    import torch
    from sentence_transformers import SentenceTransformer

    os.makedirs(note_dir, exist_ok=True)

    rows = []
    for r in notes_df.itertuples():
        for i in range(0, len(r.text), CHUNK - OVERLAP):
            piece = r.text[i:i + CHUNK].strip()
            if len(piece) > 100:
                rows.append({"subject_id": int(r.subject_id),
                             "hadm_id": int(r.hadm_id),
                             "note_id": r.note_id,
                             "offset": i,
                             "text": piece})
    ch = pd.DataFrame(rows)
    print(f"{len(ch)} chunks from {len(notes_df)} notes")

    m = SentenceTransformer(EMBEDDER,
                            device="cuda" if torch.cuda.is_available() else "cpu")
    e = m.encode(ch.text.tolist(), batch_size=64,
                 normalize_embeddings=True, show_progress_bar=True)

    ch.to_parquet(f"{note_dir}/chunks.parquet")
    np.save(f"{note_dir}/emb.npy", e)
    print(f"saved -> {note_dir}/chunks.parquet, emb.npy  {e.shape}")
    return load(note_dir)


def fetch_notes(subject_ids, project, note_dir=NOTE_DIR):
    """Pull the demo cohort's discharge summaries from BigQuery.

    Requires PhysioNet credentialing for MIMIC-IV-Note and a BigQuery access
    grant on the authenticated Google account. Authenticate first with:

        !gcloud auth application-default login --no-launch-browser
        !gcloud auth application-default set-quota-project <project>
    """
    from google.cloud import bigquery
    os.makedirs(note_dir, exist_ok=True)

    bq = bigquery.Client(project=project)
    ids = ",".join(str(int(s)) for s in subject_ids)
    df = bq.query(f"""
        SELECT subject_id, hadm_id, note_id, note_type, note_seq,
               charttime, storetime, text
        FROM `physionet-data.mimiciv_note.discharge`
        WHERE subject_id IN ({ids})
        ORDER BY subject_id, charttime
    """).to_dataframe()

    df.to_parquet(f"{note_dir}/demo_notes.parquet")
    print(f"{len(df)} notes · {df.subject_id.nunique()} patients · "
          f"{df.hadm_id.nunique()} admissions")
    return df


# ---------------------------------------------------------------------------
# Usage, once the index exists:
#
#     exec(open('pathb.py').read())
#     load()
#     show("Why was venetoclax started instead of prednisone?",
#          subject_id=10014354)
#
# First time, or after changing the chunking:
#
#     sids  = [r["p"] for r in cypher("MATCH (p:Patient) RETURN p.subject_id AS p")]
#     notes = fetch_notes(sids, project="<your-gcp-project>")
#     build_index(notes)
# ---------------------------------------------------------------------------
