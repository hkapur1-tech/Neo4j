// =============================================================
// Neo4j Browser queries — presentation order
// Patient-journey graph over MIMIC-IV Demo v2.2
// Save each block via the star icon; name it exactly as commented.
// =============================================================


// ---- 01 · Schema --------------------------------------------
// Neo4j draws the data model itself. Open with this.
CALL db.schema.visualization();


// ---- 02 · Graph scale ---------------------------------------
// 65,163 nodes total. Six labels.
MATCH (n)
RETURN labels(n)[0] AS label, count(*) AS nodes
ORDER BY nodes DESC;


// ---- 02b · Relationship scale (optional) ---------------------
// 76,409 relationships. Use if he asks about edges.
MATCH ()-[r]->()
RETURN type(r) AS relationship, count(*) AS count
ORDER BY count DESC;


// ---- 03 · Patient journey ------------------------------------
// Renders as a GRAPH. The visual to let him look at.
MATCH path = (p:Patient {subject_id: 10014354})-[:HAS_ADMISSION]->(a:Admission)-[r]->(x)
WHERE type(r) IN ['HAS_DIAGNOSIS','HAS_MEDICATION']
RETURN path LIMIT 120;


// ---- 04 · Peak creatinine ------------------------------------
// Aggregation: the first thing passage retrieval cannot do.
MATCH (p:Patient {subject_id: 10014354})-[:HAS_ADMISSION]->(a:Admission)
MATCH (a)-[:INCLUDES_LAB]->(l:LabEvent)
WHERE l.label CONTAINS 'Creatinine'
WITH a, max(l.valuenum) AS peak
ORDER BY peak DESC LIMIT 1
MATCH (a)-[:HAS_MEDICATION]->(m:Medication)
RETURN a.admittime AS admitted, peak, collect(DISTINCT m.name) AS medications;


// ---- 05 · Leukaemia trace ------------------------------------
// The payoff. 20 rows: prednisone at 2.0-2.2, switch to
// venetoclax 2148-08-22, creatinine down to 1.9 / 1.5 / 1.3.
// Rows showing [] and null survive only because of OPTIONAL MATCH.
MATCH (p:Patient {subject_id: 10014354})-[:HAS_ADMISSION]->(a)
OPTIONAL MATCH (a)-[:HAS_DIAGNOSIS]->(d:Diagnosis)
  WHERE toLower(d.long_title) CONTAINS 'lymphocytic leukemia'
OPTIONAL MATCH (a)-[:HAS_MEDICATION]->(m:Medication)
  WHERE m.name IN ['venetoclax','prednisone']
OPTIONAL MATCH (a)-[:INCLUDES_LAB]->(l:LabEvent)
  WHERE l.label CONTAINS 'Creatinine'
RETURN a.admittime AS admitted,
       CASE WHEN d.long_title CONTAINS 'in remission' THEN 'remission'
            WHEN d.long_title IS NULL                 THEN '-'
            ELSE 'active' END       AS cll,
       collect(DISTINCT m.name)     AS therapy,
       round(max(l.valuenum),1)     AS peak_creatinine
ORDER BY admitted;


// ---- 06 · Sepsis check ---------------------------------------
// Returns 0 rows, deliberately. Correct query, absent condition.
// An empty result and a wrong result look identical to a user —
// which is why the pipeline returns the executed Cypher.
MATCH (p:Patient {subject_id: 10014354})-[:HAS_ADMISSION]->(a)
MATCH (a)-[:HAS_DIAGNOSIS]->(d:Diagnosis)
WHERE toLower(d.long_title) CONTAINS 'sepsis'
RETURN a.admittime, d.long_title;
