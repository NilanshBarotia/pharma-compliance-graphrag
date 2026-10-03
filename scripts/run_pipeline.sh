#!/usr/bin/env bash
# Generic end-to-end runner for Version 1 / Point 2.
#
#   ./scripts/run_pipeline.sh                       # extract + resolve + validate only
#   ./scripts/run_pipeline.sh data/synthetic         # same, but say which doc folder explicitly
#   NEO4J_PASSWORD=... ./scripts/run_pipeline.sh --load --query
#
# To point this at a NEW document set: change INPUT_DIR (below or via $1),
# and edit config/extraction_patterns.json / config/vocab.json /
# config/clause_catalog.json. No code changes needed for new documents that
# reuse the same ontology.
set -euo pipefail
cd "$(dirname "$0")/.."

INPUT_DIR="data/synthetic"
DO_LOAD=false
DO_QUERY=false
INSECURE_TLS=""
GRAPH_FOR_LOAD=""   # defaults to the resolved graph; pass --ground-truth to load that instead

for arg in "$@"; do
  case "$arg" in
    --load) DO_LOAD=true ;;
    --query) DO_QUERY=true ;;
    --insecure-tls) INSECURE_TLS="--insecure-tls" ;;
    --ground-truth) GRAPH_FOR_LOAD="data/synthetic/ground_truth_graph.json" ;;
    *) INPUT_DIR="$arg" ;;
  esac
done

echo "== 1/4 extract =="
python3 scripts/extract.py "$INPUT_DIR" --out data/extracted/extracted_graph.json

echo "== 2/4 resolve =="
python3 scripts/resolve.py data/extracted/extracted_graph.json \
  --out data/extracted/resolved_graph.json --report data/extracted/resolve_report.json

GRAPH_FOR_LOAD="${GRAPH_FOR_LOAD:-data/extracted/resolved_graph.json}"

echo "== 3/4 validate =="
python3 scripts/validate.py "$GRAPH_FOR_LOAD"

if [ "$DO_LOAD" = true ]; then
  echo "== 4/4 load into Neo4j =="
  python3 scripts/load_neo4j.py "$GRAPH_FOR_LOAD" --wipe $INSECURE_TLS
  if [ "$DO_QUERY" = true ]; then
    echo "== running cypher/queries.cypher =="
    python3 scripts/run_queries.py cypher/queries.cypher $INSECURE_TLS
  fi
else
  echo "== 4/4 load into Neo4j: skipped (pass --load, with NEO4J_PASSWORD set) =="
fi
