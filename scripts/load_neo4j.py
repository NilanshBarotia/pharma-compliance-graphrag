#!/usr/bin/env python3
"""Load an ontology-conformant graph document into Neo4j. Generic: works for
any node labels / relationship types, as long as they match the schema
(relationship type names come from the DATA, not hardcoded here -- they are
only checked against the schema's allow-list before being interpolated into
Cypher, since Neo4j has no parameter placeholder for relationship types).

Connection is via environment variables (or --uri/--user/--password):
  NEO4J_URI       default bolt://localhost:7687
  NEO4J_USER      default neo4j
  NEO4J_PASSWORD  required (or --password)
  NEO4J_DATABASE  default neo4j

Idempotent: re-running with the same graph updates properties in place
(MERGE on label+id) rather than duplicating nodes/edges. Re-running with an
updated graph (e.g. after re-extraction) is therefore safe.

If your network or laptop has TLS-inspecting security software (common on
institutional/campus-managed devices -- it substitutes its own certificate
for every encrypted connection, including Aura's), you'll see:
  ssl.SSLCertVerificationError: ... self signed certificate in certificate chain
Pass --insecure-tls to trust the connection anyway. This does NOT disable
Aura's own encryption -- your traffic is still encrypted -- it just stops
Python from also verifying the substituted certificate's identity, which is
appropriate here since you already know which database you're talking to
(you typed the URI yourself).

Usage:
  export NEO4J_PASSWORD=...
  python scripts/load_neo4j.py data/extracted/resolved_graph.json
  python scripts/load_neo4j.py data/synthetic/ground_truth_graph.json --wipe
  python scripts/load_neo4j.py graph.json --dry-run     # no DB needed; just prints statements + counts
  python scripts/load_neo4j.py graph.json --insecure-tls  # campus/managed-laptop TLS interception workaround
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "ontology" / "ontology.schema.json"
VALID_ID = re.compile(r"^[A-Za-z0-9._()\-]+$")


def allowed_types(schema):
    labels = {k[5:] for k in schema["$defs"] if k.startswith("node_")}
    rels = {k[5:] for k in schema["$defs"] if k.startswith("edge_")}
    return labels, rels


def node_statement(label, id_, props):
    # Every property value is passed as a parameter; only the (validated) label is interpolated.
    return f"MERGE (n:`{label}` {{id: $id}}) SET n += $props", {"id": id_, "props": props}


def edge_statement(rel_type, from_label, from_id, to_label, to_id, props):
    q = (f"MATCH (a:`{from_label}` {{id: $from_id}}), (b:`{to_label}` {{id: $to_id}}) "
         f"MERGE (a)-[r:`{rel_type}`]->(b) SET r += $props")
    return q, {"from_id": from_id, "to_id": to_id, "props": props}


def flatten_node_props(node):
    props = {k: v for k, v in node.items() if k not in ("label", "id")}
    if "sources" in props:
        props["sources_json"] = json.dumps(props.pop("sources"))
    if "aliases" in props:
        props["aliases"] = list(props["aliases"])
    for k, v in list(props.items()):
        if isinstance(v, dict):
            props[k] = json.dumps(v)  # Neo4j properties can't be nested maps
    return props


def flatten_edge_props(edge):
    props = dict(edge.get("properties", {}))
    props["sources_json"] = json.dumps(edge.get("sources", []))
    return props


def build_statements(graph, labels, rels):
    problems, statements = [], []
    seen_ids = set()
    for n in graph["nodes"]:
        if n["label"] not in labels:
            problems.append(f"unknown label {n['label']!r} (id {n['id']}) - not in ontology schema, skipped")
            continue
        if not VALID_ID.match(n["id"]):
            problems.append(f"id {n['id']!r} has unexpected characters, skipped")
            continue
        seen_ids.add((n["label"], n["id"]))
        statements.append(("node", node_statement(n["label"], n["id"], flatten_node_props(n))))
    for e in graph["edges"]:
        if e["type"] not in rels:
            problems.append(f"unknown relationship type {e['type']!r} - not in ontology schema, skipped")
            continue
        fl, fid, tl, tid = e["from"]["label"], e["from"]["id"], e["to"]["label"], e["to"]["id"]
        if (fl, fid) not in seen_ids or (tl, tid) not in seen_ids:
            problems.append(f"edge {e['type']} {fid}->{tid}: endpoint not among loaded nodes, skipped")
            continue
        statements.append(("edge", edge_statement(e["type"], fl, fid, tl, tid, flatten_edge_props(e))))
    return statements, problems


def run(statements, uri, user, password, database, wipe, insecure_tls=False):
    from neo4j import GraphDatabase
    if insecure_tls:
        from neo4j import TrustAll
        uri = uri.replace("neo4j+s://", "neo4j://").replace("bolt+s://", "bolt://")
        driver = GraphDatabase.driver(uri, auth=(user, password), encrypted=True, trusted_certificates=TrustAll())
    else:
        driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session(database=database) as session:
        if wipe:
            session.run("MATCH (n) DETACH DELETE n")
        # constraints: one per label seen, generic (label name is validated above)
        labels_seen = {q[0].split("`")[1] for kind, q in statements if kind == "node"}
        for label in labels_seen:
            session.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:`{label}`) REQUIRE n.id IS UNIQUE")
        counts = {"node": 0, "edge": 0}
        for kind, (cypher, params) in statements:
            session.run(cypher, **params)
            counts[kind] += 1
    driver.close()
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("graph_path")
    ap.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    ap.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    ap.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    ap.add_argument("--database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    ap.add_argument("--wipe", action="store_true", help="DETACH DELETE all nodes before loading")
    ap.add_argument("--dry-run", action="store_true", help="build statements and report counts; skip DB")
    ap.add_argument("--insecure-tls", action="store_true",
                     help="trust the TLS connection without verifying the certificate identity "
                          "(workaround for campus/managed-laptop networks that substitute their own certificate)")
    args = ap.parse_args()

    schema = json.loads(SCHEMA.read_text())
    labels, rels = allowed_types(schema)
    graph = json.loads(Path(args.graph_path).read_text())
    statements, problems = build_statements(graph, labels, rels)

    for p in problems:
        print(f"[load] SKIP: {p}", file=sys.stderr)
    n_nodes = sum(1 for k, _ in statements if k == "node")
    n_edges = sum(1 for k, _ in statements if k == "edge")
    print(f"[load] prepared {n_nodes} node MERGEs, {n_edges} edge MERGEs "
          f"({len(problems)} skipped) against {args.uri}" + (" [dry run]" if args.dry_run else ""))

    if args.dry_run:
        for kind, (cypher, params) in statements[:3]:
            print(f"  e.g. {cypher}  params={params}")
        return

    if not args.password:
        print("[load] error: no Neo4j password given (set NEO4J_PASSWORD or pass --password)", file=sys.stderr)
        sys.exit(1)
    try:
        counts = run(statements, args.uri, args.user, args.password, args.database, args.wipe, args.insecure_tls)
    except ImportError:
        print("[load] error: the `neo4j` Python driver is not installed. `pip install neo4j`", file=sys.stderr)
        sys.exit(1)
    print(f"[load] done: {counts['node']} node merges, {counts['edge']} edge merges executed")


if __name__ == "__main__":
    main()
