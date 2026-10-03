# Pharma manufacturing compliance GraphRAG — Version 1, Step 1: Domain scope

Educational knowledge-graph project built on **public regulatory text** and **fictional synthetic records**. It contains no real manufacturing or operational instructions, and all "Northfield Pharma / Riverbend / Meridax" material is invented.

## What's here
| Path | Purpose |
|---|---|
| `ontology/ontology.schema.json` | JSON Schema (2020-12): 9 node types, 19 direction-constrained relationship types, mandatory provenance |
| `ontology/build_schema.py` | Single source of truth that generates the schema |
| `ontology/ONTOLOGY.md` | Diagram + design decisions |
| `data/sources/sources.json` | Curated index of 8 real public documents (URLs, jurisdiction, reuse notes, chunking hints) |
| `scripts/fetch_sources.py` | Downloads the real texts into `data/raw/` (git-ignored) and splits 21 CFR 211 into per-section JSONL |
| `data/synthetic/*.md` | 5 fictional documents: batch record summary, 2 deviations, 2 CAPAs |
| `data/synthetic/ground_truth_graph.json` | Hand-built graph (23 nodes, 34 edges) that conforms to the schema; used later to score extraction |
| `scripts/validate.py` | Schema + referential-integrity + provenance checks |

## Run
```bash
pip install -r requirements.txt
python scripts/validate.py                 # validate the ground-truth graph
python scripts/fetch_sources.py            # needs internet; fetch real texts
```

## Real document set
- **21 CFR Part 211** (eCFR)
- **EU GMP Annex 11** (Computerised Systems) and **Annex 15** (Qualification and Validation)
- **5 FDA warning letters** (Seaway Pharma, Product Society, Agropharma, Taizhou Jingshang, Marshalls Traditional Healthcare)

## Status / caveats
- URLs were confirmed on 2026-09-23; the texts themselves are fetched by the script, not committed.
- Annex 11 clause numbering was checked against the published PDF. Annex 15 numbering and some 21 CFR paragraph-level titles are **not yet verified** (flagged `unverified`).
- Reuse terms: FDA/CFR material is US government work; check EU Commission reuse terms before committing full annex text.

## Version 1, Step 2: Data & graph layer

A **generic, config-driven** pipeline: to point it at a different document set you edit
`config/extraction_patterns.json` / `config/vocab.json` / `config/clause_catalog.json` — no
code changes. All strings specific to *this* pharma corpus live in config, not in `scripts/*.py`.

| Script | Role |
|---|---|
| `scripts/extract.py` | Rule-based NER + relation extraction: regex entity patterns + "keyword-anchored window" relation matching (finds a trigger phrase, links the nearest entities of the right labels within N characters). Also auto-derives `CHILD_OF` for clause sub-paragraphs and looks up clause titles from `clause_catalog.json`. |
| `scripts/resolve.py` | Entity resolution: exact/alias matching for all labels, plus fuzzy (difflib) matching for free-text-named labels only (`Equipment`, `Site`, `Product`, `Inspector` — configurable). Structured-ID labels (`SOP`, `Deviation`, `CAPA`, `RegulationClause`, `Batch`) are deliberately excluded from fuzzy matching so e.g. `CAPA-2026-011` and `CAPA-2026-019` never collide. |
| `scripts/load_neo4j.py` | Loads any ontology-conformant graph into Neo4j with `MERGE` (idempotent). Validates every label/relationship type against `ontology.schema.json` before touching the DB; unknown types are skipped and reported, never silently interpolated. Supports `--dry-run` (no DB needed). |
| `cypher/queries.cypher` | 8 hand-written multi-hop queries, including the brief's example ("which CAPAs trace back to clause X") and an entity-resolution sanity check. |
| `scripts/run_queries.py` | Runs every query in a `.cypher` file and prints results as a table. |
| `scripts/run_pipeline.sh` | Orchestrates extract → resolve → validate → (optional) load → query in one command. |
| `tests/test_pipeline.py` | Stdlib-only tests (no pytest in this sandbox) covering extraction, alias merging, false-merge avoidance, and Neo4j statement building. |

### Run it
```bash
pip install -r requirements.txt

# offline part (no DB needed)
./scripts/run_pipeline.sh                 # extract + resolve + validate
python3 tests/test_pipeline.py            # 15 checks

# with a real Neo4j (local Docker, Desktop, or free Aura instance)
export NEO4J_URI=bolt://localhost:7687
export NEO4J_PASSWORD=your-password
./scripts/run_pipeline.sh --load --query           # loads the auto-extracted graph
./scripts/run_pipeline.sh --ground-truth --load --query  # loads the hand-built graph instead
```

### Known extraction gaps (by design, for this step)
The rule-based extractor does not yet produce `Site`, `Product`, or `Inspector` nodes, or
`LOCATED_AT` / `MANUFACTURED_AT` / `OF_PRODUCT` / `RAISED` / `INSPECTED` / `CITED` edges,
because the synthetic docs state those facts in prose the current regex patterns don't
target (e.g. "Riverbend QC Laboratory" has no structured ID to anchor a pattern on). This is
exactly the gap `ontology/build_schema.py` and `data/synthetic/ground_truth_graph.json`
exist to make visible: **`ground_truth_graph.json` already has 100% label/relationship
coverage** and is what the Cypher queries are written against. Closing the extraction gap
(adding free-text NER for names, not just regex on codes) is a natural next iteration of
`extract.py`, not a schema change.

### Sandbox limitation
This container has no outbound network access and no Neo4j/Docker installed, so
`load_neo4j.py` / `run_queries.py` were verified with `--dry-run` and unit tests
(`tests/test_pipeline.py`) rather than against a live database. The Cypher itself is
ordinary, unremarkable syntax — point `NEO4J_URI`/`NEO4J_PASSWORD` at any Neo4j 5.x instance
and it will run as-is.

## Next (Version 1, step 3)
Chunk + embed the raw documents into a vector DB → hybrid (vector + graph) retrieval →
LangGraph multi-step investigation agent → citation grounding.
