#!/usr/bin/env python3
"""Run every query in a .cypher file against Neo4j and print results.

Generic: splits on statement-terminating ';', skips comment-only chunks
(lines starting with //), and labels each result block with the last
comment block that preceded it (so cypher/queries.cypher's own
descriptions become the printed headers).

Usage:
  export NEO4J_PASSWORD=...
  python scripts/run_queries.py cypher/queries.cypher
  python scripts/run_queries.py cypher/queries.cypher --only 1 4   # 1-indexed
  python scripts/run_queries.py cypher/queries.cypher --insecure-tls  # campus/managed-laptop TLS interception workaround
"""
import argparse
import os
import re
import sys
from pathlib import Path


def split_statements(cypher_text):
    """Return [(comment_header, statement), ...] preserving order."""
    blocks, comment_buf, stmt_buf = [], [], []
    for line in cypher_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            comment_buf.append(stripped.lstrip("/ "))
            continue
        if not stripped:
            continue
        stmt_buf.append(line)
        if stripped.endswith(";"):
            blocks.append(("\n".join(comment_buf).strip(), "\n".join(stmt_buf).rstrip(";").strip()))
            stmt_buf = []
            comment_buf = []  # next comment run describes the NEXT statement, not this one
    return blocks


def print_table(records, keys):
    widths = [max(len(k), *(len(str(r.get(k, ""))) for r in records)) if records else len(k) for k in keys]
    print(" | ".join(k.ljust(w) for k, w in zip(keys, widths)))
    print("-+-".join("-" * w for w in widths))
    for r in records:
        print(" | ".join(str(r.get(k, "")).ljust(w) for k, w in zip(keys, widths)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cypher_file")
    ap.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    ap.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    ap.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    ap.add_argument("--database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    ap.add_argument("--only", nargs="*", type=int, help="1-indexed query numbers to run")
    ap.add_argument("--insecure-tls", action="store_true",
                     help="trust the TLS connection without verifying the certificate identity "
                          "(workaround for campus/managed-laptop networks that substitute their own certificate)")
    args = ap.parse_args()

    blocks = split_statements(Path(args.cypher_file).read_text())
    if args.only:
        blocks = [b for i, b in enumerate(blocks, 1) if i in args.only]
    if not args.password:
        print("error: set NEO4J_PASSWORD (or --password)", file=sys.stderr)
        sys.exit(1)

    from neo4j import GraphDatabase
    if args.insecure_tls:
        from neo4j import TrustAll
        uri = args.uri.replace("neo4j+s://", "neo4j://").replace("bolt+s://", "bolt://")
        driver = GraphDatabase.driver(uri, auth=(args.user, args.password), encrypted=True, trusted_certificates=TrustAll())
    else:
        driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    with driver.session(database=args.database) as session:
        for i, (header, stmt) in enumerate(blocks, 1):
            title = re.search(r"Q\d+\..*", header)
            print(f"\n=== Query {i}: {title.group(0) if title else header.splitlines()[0] if header else ''} ===")
            result = session.run(stmt)
            records = [r.data() for r in result]
            if not records:
                print("(no rows)")
                continue
            print_table(records, list(records[0].keys()))
    driver.close()


if __name__ == "__main__":
    main()
