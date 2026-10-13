"""
arrows.py — the schema as one artifact, used twice.

THE PROPOSAL
  The Text2Cypher prompt currently carries a hand-typed schema string. A
  hand-typed string can describe a graph that does not exist: a property
  renamed in the loader and not in the prompt produces confident Cypher
  against a field that isn't there. The proposal is to author the schema once,
  as a diagram in Arrows (arrows.app), and derive both the paper figure and
  the prompt text from that single export.

WHY IT HAS TO BE MEASURED, NOT ASSUMED
  The schema ablation on the demo graph showed that a four-line note about
  property types moved H08 from 0% to 100% and simultaneously moved H01 from
  100% to 25% — a negation question that mentions no dates. Prompt edits are
  non-local and do not scale with edit size. Replacing the whole schema
  description is a much larger edit than that, so "it should be equivalent"
  is not a claim available without a before and after on the same graph.

THREE ARMS
  A  hand-written    the current string: structure, property types, and prose
                     notes (fluid ambiguity, is_dka, seq_num = 1 semantics)
  B  arrows only     structure and property types derived from the export,
                     no prose
  C  arrows + notes  B with arm A's prose notes appended

  A vs B asks whether the diagram alone suffices. B vs C separates the value
  of the prose from the value of the structural format, which A vs B alone
  confounds.

ROUND TRIP
  introspect(run)         read the live graph's actual labels, property types
                          and relationship endpoints
  to_arrows(s, path)      write Arrows-importable JSON
  -> arrows.app           Open > From JSON, lay out, export SVG for the paper
                          and JSON back out
  from_arrows(path)       the exported JSON becomes the prompt string

  Introspecting first means the diagram cannot describe a schema the database
  does not have. Verify the JSON shape before generating the full file: draw
  two nodes in Arrows, export, and check the keys against ARROWS_KEYS below —
  the app's format has changed across versions.
"""

import json

# Arrows JSON, as of writing. Confirm against a hand-drawn two-node export.
ARROWS_KEYS = {
    "top":           ["nodes", "relationships", "style"],
    "node":          ["id", "position", "caption", "labels", "properties", "style"],
    "relationship":  ["id", "type", "fromId", "toId", "properties", "style"],
}

# Rough layout so the import is readable rather than a pile at the origin.
# Patient left, Admission as the hub, everything else radiating right.
LAYOUT = {
    "Patient":    (-600,    0),
    "Admission":  (-200,    0),
    "ICUStay":    ( 200, -320),
    "Diagnosis":  ( 200, -110),
    "Procedure":  ( 200,  110),
    "Medication": ( 200,  320),
    "LabEvent":   ( 560,    0),
}

# Prose that no diagram carries. Arm A has it, arm B does not, arm C adds it
# back. Each line exists because a question failed without it.
NOTES = """
Cohort: 96 patients with three or more admissions whose principal diagnosis is
diabetic ketoacidosis. 1,979 admissions in total; is_dka marks the 552 that are
DKA admissions.

LabEvents exist only for DKA admissions, and 34 DKA admissions have no panel
lab at all, so a per-admission lab question covers 518 admissions rather
than 552.

HAS_DIAGNOSIS.seq_num = 1 is the principal diagnosis. The same code can be
principal for one admission and secondary for another, which is why seq_num is
on the relationship and not the node.

LabEvent.label is ambiguous without fluid: Glucose is recorded in both Blood
and Urine, and pH in Blood, Urine and Other Body Fluid.
""".strip()


# ===========================================================================
# READ THE LIVE GRAPH
# ===========================================================================

def introspect(run, verbose=True):
    """Actual labels, property types and relationship endpoints.

    Uses the built-in procedures rather than APOC, and reads endpoints by
    scanning distinct label triples — exact, and a few seconds on this graph.
    """
    nodes = {}
    for r in run("CALL db.schema.nodeTypeProperties()"):
        label = r["nodeLabels"][0]
        types = r.get("propertyTypes") or []
        nodes.setdefault(label, {})[r["propertyName"]] = _t(types)

    relprops = {}
    for r in run("CALL db.schema.relTypeProperties()"):
        t = r["relType"].strip(":`")
        types = r.get("propertyTypes") or []
        if r["propertyName"]:
            relprops.setdefault(t, {})[r["propertyName"]] = _t(types)

    edges = []
    for r in run("""
        MATCH (a)-[e]->(b)
        RETURN DISTINCT labels(a)[0] AS src, type(e) AS type, labels(b)[0] AS dst
        ORDER BY src, type
    """):
        edges.append({"src": r["src"], "type": r["type"], "dst": r["dst"],
                      "properties": relprops.get(r["type"], {})})

    s = {"nodes": nodes, "edges": edges}
    if verbose:
        for label, props in sorted(nodes.items()):
            print(f"  (:{label})  {', '.join(sorted(props))}")
        print()
        for e in edges:
            p = f" {{{', '.join(sorted(e['properties']))}}}" if e["properties"] else ""
            print(f"  (:{e['src']})-[:{e['type']}{p}]->(:{e['dst']})")
    return s


_TYPE = {"Long": "int", "Double": "float", "String": "str",
         "Boolean": "bool", "DateTime": "datetime", "Date": "date",
         "LocalDateTime": "datetime"}


def _t(types):
    """Neo4j's reported property types -> the short names used in the prompt."""
    if not types:
        return "any"
    return "|".join(sorted({_TYPE.get(t, t.lower()) for t in types}))


# ===========================================================================
# WRITE FOR ARROWS
# ===========================================================================

def to_arrows(s, path="schema_dka.arrows.json"):
    """Arrows-importable JSON. Open it with arrows.app > Open > From JSON.

    Property VALUES carry the type name, because Arrows has no type field —
    it stores properties as name/value strings. So `valuenum: float` in the
    drawing is the type annotation, and from_arrows() reads it straight back.
    That is a deliberate convention, and it is the reason the round trip
    preserves types at all.
    """
    nodes, idx = [], {}
    for i, (label, props) in enumerate(sorted(s["nodes"].items())):
        nid = f"n{i}"
        idx[label] = nid
        x, y = LAYOUT.get(label, (i * 240, 0))
        nodes.append({"id": nid,
                      "position": {"x": x, "y": y},
                      "caption": "",
                      "labels": [label],
                      "properties": dict(sorted(props.items())),
                      "style": {}})

    rels = []
    for i, e in enumerate(s["edges"]):
        if e["src"] not in idx or e["dst"] not in idx:
            continue
        rels.append({"id": f"r{i}",
                     "type": e["type"],
                     "fromId": idx[e["src"]],
                     "toId": idx[e["dst"]],
                     "properties": dict(sorted(e["properties"].items())),
                     "style": {}})

    doc = {"style": {}, "nodes": nodes, "relationships": rels}
    json.dump(doc, open(path, "w"), indent=2)
    print(f"{len(nodes)} nodes · {len(rels)} relationships -> {path}")
    print("arrows.app > Open > From JSON, then Export for the SVG and the JSON")
    return doc


# ===========================================================================
# READ BACK AS THE PROMPT STRING
# ===========================================================================

def from_arrows(path, notes=None):
    """Arrows export -> the schema string for the Text2Cypher prompt.

    `notes=None` is arm B: structure and types only, nothing a diagram cannot
    hold. `notes=NOTES` is arm C. Arm A is the hand-written string in
    dka_build.SCHEMA.
    """
    doc = json.load(open(path))
    by_id = {n["id"]: n for n in doc["nodes"]}

    lines = []
    for n in doc["nodes"]:
        label = (n.get("labels") or ["?"])[0]
        props = ", ".join(f"{k}:{v}" for k, v in n.get("properties", {}).items())
        lines.append(f"(:{label} {{{props}}})")

    lines.append("")
    for r in doc["relationships"]:
        src = (by_id[r["fromId"]].get("labels") or ["?"])[0]
        dst = (by_id[r["toId"]].get("labels") or ["?"])[0]
        props = ", ".join(f"{k}:{v}" for k, v in r.get("properties", {}).items())
        mid = f":{r['type']} {{{props}}}" if props else f":{r['type']}"
        lines.append(f"(:{src})-[{mid}]->(:{dst})")

    out = "\n".join(lines)
    if notes:
        out += "\n\n" + notes.strip()
    return out


def validate(run, path, verbose=True):
    """Does the hand-drawn Arrows model describe the graph that exists?

    The schema is authored as a drawing, so nothing stops it from naming a
    property the loader never writes or omitting one it does. That is the same
    failure a hand-typed schema string has, and it is silent: the model
    generates confident Cypher against a field that is not there, the query
    runs, and it returns nothing or something wrong.

    Run this after every export. Drift found here is a result worth reporting,
    not just a bug to fix quietly.
    """
    drawn = json.load(open(path))
    live = introspect(run, verbose=False)

    d_nodes = {(n.get("labels") or ["?"])[0]: set(n.get("properties", {}))
               for n in drawn["nodes"]}
    l_nodes = {k: set(v) for k, v in live["nodes"].items()}

    by_id = {n["id"]: (n.get("labels") or ["?"])[0] for n in drawn["nodes"]}
    d_edges = {(by_id[r["fromId"]], r["type"], by_id[r["toId"]])
               for r in drawn["relationships"]}
    l_edges = {(e["src"], e["type"], e["dst"]) for e in live["edges"]}

    problems = []
    for label in sorted(set(d_nodes) | set(l_nodes)):
        if label not in l_nodes:
            problems.append(f"(:{label}) is drawn but does not exist")
            continue
        if label not in d_nodes:
            problems.append(f"(:{label}) exists but is not drawn")
            continue
        for p in sorted(d_nodes[label] - l_nodes[label]):
            problems.append(f"(:{label}) drawn property `{p}` does not exist")
        for p in sorted(l_nodes[label] - d_nodes[label]):
            problems.append(f"(:{label}) real property `{p}` is not drawn")

    for e in sorted(d_edges - l_edges):
        problems.append(f"{e[0]}-[:{e[1]}]->{e[2]} is drawn but does not exist")
    for e in sorted(l_edges - d_edges):
        problems.append(f"{e[0]}-[:{e[1]}]->{e[2]} exists but is not drawn")

    if verbose:
        if problems:
            print(f"{len(problems)} discrepancies between drawing and database\n")
            for p in problems:
                print("  " + p)
        else:
            print("drawing matches the database exactly")
    return problems


def diff(a, b, label_a="A hand-written", label_b="B arrows"):
    """Line-level difference between two schema strings.

    Read this before measuring. If the two strings are materially the same,
    the experiment has nothing to detect and a null result means only that
    you compared a string with itself.
    """
    import difflib
    la, lb = a.strip().splitlines(), b.strip().splitlines()
    print(f"{label_a}: {len(la)} lines, {len(a)} chars")
    print(f"{label_b}: {len(lb)} lines, {len(b)} chars\n")
    for line in difflib.unified_diff(la, lb, label_a, label_b, lineterm="", n=1):
        print(line)


# ---------------------------------------------------------------------------
# Usage — drawing first, which is the Arrows workflow:
#
#   1. Draw the seven labels and six relationship types at arrows.app by hand.
#      Put the TYPE NAME in each property's value slot (valuenum: float), so
#      the export carries types.
#   2. Export three times: SVG for the paper, JSON for the schema, Cypher for
#      the CREATE-statement arm.
#   3. Check the drawing against the graph that exists:
#
#        exec(open('arrows.py').read())
#        validate(run, 'schema_dka.arrows.json')
#
#      Anything it prints is drift between what you drew and what you built.
#      Fix the drawing, re-export, run it again until it is clean.
#
#   4. Build the arms and confirm they actually differ:
#
#        SCHEMA_B = from_arrows('schema_dka.arrows.json')
#        SCHEMA_D = from_arrows('schema_dka.arrows.json', notes=NOTES)
#        SCHEMA_C = open('schema_dka.cypher').read()   # Arrows Cypher export
#        diff(SCHEMA, SCHEMA_B)                        # SCHEMA = arm A
#
#   A  hand-written string (dka_build.SCHEMA)
#   B  Arrows JSON -> schema description
#   C  Arrows Cypher export, passed as-is
#   D  B plus the prose notes
#
# All four go through the 44-question bank unchanged, five runs each, both
# scoring criteria. Nothing else in the prompt may differ between arms.
#
# introspect() + to_arrows() remain available for the other direction — useful
# for seeding a first draft, or for redrawing after a schema change — but the
# authored artifact is the drawing.
# ---------------------------------------------------------------------------
