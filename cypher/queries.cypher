// Hand-written Cypher queries, Version 1 / Point 2, last item:
// "confirm the graph actually answers multi-hop questions."
//
// Run against the GROUND-TRUTH graph for guaranteed results:
//   python scripts/load_neo4j.py data/synthetic/ground_truth_graph.json --wipe
// (the auto-extracted graph is missing Site/Product/Inspector nodes and
// several relation types -- see README's "known extraction gaps" -- so
// queries 5-8 below will return fewer rows against it.)
//
// Open these in Neo4j Browser / cypher-shell, or run all of them at once
// with scripts/run_queries.py.

// -----------------------------------------------------------------------
// Q1. Which CAPAs trace back to a given regulation clause? (the example
//     question from the brief) CAPA -> Deviation -> RegulationClause,
//     and/or CAPA -[REFERENCES_CLAUSE]-> RegulationClause directly.
// -----------------------------------------------------------------------
MATCH (clause:RegulationClause {citation: '21 CFR 211.68(a)'})
OPTIONAL MATCH (capa:CAPA)-[:REFERENCES_CLAUSE]->(clause)
OPTIONAL MATCH (capa2:CAPA)-[:ADDRESSES]->(:Deviation)-[:RELATES_TO_CLAUSE]->(clause)
RETURN clause.citation AS clause,
       collect(DISTINCT capa.capa_number) + collect(DISTINCT capa2.capa_number) AS capas_tracing_back;

// -----------------------------------------------------------------------
// Q2. Full multi-hop investigation path: deviation -> equipment ->
//     other deviations on the same equipment -> their CAPAs -> clauses.
//     (This is the exact chain named in the brief: "deviation -> linked
//     equipment -> related past CAPAs -> applicable regulation clause".)
// -----------------------------------------------------------------------
MATCH (d:Deviation {deviation_number: 'DEV-2026-031'})-[:INVOLVES_EQUIPMENT]->(eq:Equipment)
OPTIONAL MATCH (eq)<-[:INVOLVES_EQUIPMENT]-(other:Deviation)
OPTIONAL MATCH (other)<-[:ADDRESSES]-(capa:CAPA)
OPTIONAL MATCH (capa)-[:REFERENCES_CLAUSE]->(clause:RegulationClause)
RETURN d.deviation_number AS start_deviation, eq.name AS equipment,
       collect(DISTINCT other.deviation_number) AS deviations_on_same_equipment,
       collect(DISTINCT capa.capa_number) AS resulting_capas,
       collect(DISTINCT clause.citation) AS applicable_clauses;

// -----------------------------------------------------------------------
// Q3. For a batch, show every SOP that governed it or its equipment, and
//     which regulation clause each SOP implements.
// -----------------------------------------------------------------------
MATCH (b:Batch {batch_number: 'MX26-014'})
OPTIONAL MATCH (b)-[:GOVERNED_BY]->(sopB:SOP)
OPTIONAL MATCH (b)-[:PROCESSED_ON]->(:Equipment)-[:COVERED_BY]->(sopE:SOP)
WITH b, collect(DISTINCT sopB) + collect(DISTINCT sopE) AS sops
UNWIND sops AS sop
OPTIONAL MATCH (sop)-[:IMPLEMENTS]->(clause:RegulationClause)
RETURN b.batch_number AS batch, sop.sop_number AS sop, sop.title AS sop_title,
       collect(DISTINCT clause.citation) AS clauses_implemented
ORDER BY sop;

// -----------------------------------------------------------------------
// Q4. Entity-resolution sanity check: prove one Equipment node absorbed
//     multiple surface aliases (TP-03 / "Press #3" / "Compression Line 3
//     press") and show everything now hanging off that single node.
// -----------------------------------------------------------------------
MATCH (eq:Equipment {equipment_tag: 'TP-03'})
OPTIONAL MATCH (eq)<-[:PROCESSED_ON]-(b:Batch)
OPTIONAL MATCH (eq)<-[:INVOLVES_EQUIPMENT]-(d:Deviation)
RETURN eq.name AS canonical_name, eq.aliases AS known_aliases,
       collect(DISTINCT b.batch_number) AS batches_using_it,
       collect(DISTINCT d.deviation_number) AS deviations_involving_it;

// -----------------------------------------------------------------------
// Q5. Which site had an inspection that cited a given clause, and by whom?
//     Inspector -> Site, Inspector -> RegulationClause (same visit).
// -----------------------------------------------------------------------
MATCH (insp:Inspector)-[:CITED]->(clause:RegulationClause {citation: 'EU GMP Annex 11, clause 9'})
MATCH (insp)-[:INSPECTED]->(site:Site)
RETURN clause.citation AS clause, insp.name AS inspector, insp.inspector_type AS inspector_type,
       site.name AS site;

// -----------------------------------------------------------------------
// Q6. Open items: every deviation still open/under investigation, with its
//     site, equipment and the CAPA (if any) meant to close it out.
// -----------------------------------------------------------------------
MATCH (d:Deviation)
WHERE d.status IN ['open', 'under_investigation']
OPTIONAL MATCH (d)-[:OCCURRED_AT]->(site:Site)
OPTIONAL MATCH (d)-[:INVOLVES_EQUIPMENT]->(eq:Equipment)
OPTIONAL MATCH (capa:CAPA)-[:ADDRESSES]->(d)
RETURN d.deviation_number AS deviation, d.status AS status, site.name AS site,
       eq.name AS equipment, collect(DISTINCT capa.capa_number) AS open_capas
ORDER BY deviation;

// -----------------------------------------------------------------------
// Q7. Clause hierarchy: does any deviation relate to a specific
//     sub-clause whose PARENT clause is also independently referenced
//     elsewhere in the graph? (tests CHILD_OF traversal)
// -----------------------------------------------------------------------
MATCH (child:RegulationClause)-[:CHILD_OF]->(parent:RegulationClause)
OPTIONAL MATCH (d:Deviation)-[:RELATES_TO_CLAUSE]->(child)
OPTIONAL MATCH (other:CAPA)-[:REFERENCES_CLAUSE]->(parent)
RETURN parent.citation AS parent_clause, child.citation AS child_clause,
       collect(DISTINCT d.deviation_number) AS deviations_citing_child,
       collect(DISTINCT other.capa_number) AS capas_citing_parent_directly;

// -----------------------------------------------------------------------
// Q8. Cross-framework check: list every RegulationClause actually linked
//     to at least one Deviation or CAPA, grouped by framework -- a proxy
//     for "which parts of the regs actually show up in real findings".
// -----------------------------------------------------------------------
MATCH (clause:RegulationClause)
WHERE EXISTS { (:Deviation)-[:RELATES_TO_CLAUSE]->(clause) }
   OR EXISTS { (:CAPA)-[:REFERENCES_CLAUSE]->(clause) }
RETURN clause.framework AS framework, collect(DISTINCT clause.citation) AS clauses_in_use
ORDER BY framework;
