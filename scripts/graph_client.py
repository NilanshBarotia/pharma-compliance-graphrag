#!/usr/bin/env python3
"""Generic graph access layer. Two interchangeable backends behind one
interface, so the agent (agent.py) never needs to know which is in use:

  - LocalJsonGraph: loads an ontology-conformant graph.json (e.g.
    data/synthetic/ground_truth_graph.json or data/extracted/resolved_graph.json)
    into memory and answers neighbor/multi-hop queries with plain Python.
    No DB needed -- this is the default, so the agent runs fully offline.

  - Neo4jGraph: runs the same queries against a live Neo4j instance
    (reuses the --insecure-tls workaround from load_neo4j.py). Use this
    once you have Neo4j loaded and want the agent to read live data
    instead of a static JSON snapshot.

Both expose:
  find_nodes(name_contains=None, label=None)   -> list of node dicts
  get_node(node_id)                             -> node dict or None
  neighbors(node_id, rel_type=None, direction='out'|'in'|'any') -> list of (edge_type, direction, node_dict)
  multi_hop(start_id, hop_specs)                 -> list of paths; each path is
                                                     a list of (node_dict, edge_type_or_None)
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class LocalJsonGraph:
    def __init__(self, graph_path):
        g = json.loads(Path(graph_path).read_text())
        self.nodes = {n["id"]: n for n in g["nodes"]}
        self.edges = g["edges"]
        self._out = {}
        self._in = {}
        for e in self.edges:
            self._out.setdefault(e["from"]["id"], []).append(e)
            self._in.setdefault(e["to"]["id"], []).append(e)

    def find_nodes(self, name_contains=None, label=None, doc_id=None):
        out = []
        for n in self.nodes.values():
            if label and n["label"] != label:
                continue
            if doc_id and not any(s.get("doc_id") == doc_id for s in n.get("sources", [])):
                continue
            if name_contains:
                hay = (n.get("name", "") + " " + " ".join(n.get("aliases", []))).lower()
                if name_contains.lower() not in hay:
                    continue
            out.append(n)
        return out

    def get_node(self, node_id):
        return self.nodes.get(node_id)

    def neighbors(self, node_id, rel_type=None, direction="any"):
        results = []
        if direction in ("out", "any"):
            for e in self._out.get(node_id, []):
                if rel_type and e["type"] != rel_type:
                    continue
                nb = self.nodes.get(e["to"]["id"])
                if nb:
                    results.append((e["type"], "out", nb))
        if direction in ("in", "any"):
            for e in self._in.get(node_id, []):
                if rel_type and e["type"] != rel_type:
                    continue
                nb = self.nodes.get(e["from"]["id"])
                if nb:
                    results.append((e["type"], "in", nb))
        return results

    def multi_hop(self, start_id, hop_specs):
        """hop_specs: list of {"rel": "TYPE", "direction": "out"|"in"|"any"}.
        Returns every path that successfully follows all hops, as a list of
        (node_dict, edge_type_taken_to_reach_it) tuples, starting with the
        start node (edge_type=None)."""
        start = self.get_node(start_id)
        if not start:
            return []
        paths = [[(start, None)]]
        for spec in hop_specs:
            next_paths = []
            for path in paths:
                visited = {n["id"] for n, _ in path}
                last_node = path[-1][0]
                for rel_type, _, nb in self.neighbors(last_node["id"], spec.get("rel"), spec.get("direction", "any")):
                    if nb["id"] in visited:
                        continue  # avoid A -> B -> A revisits (cycle noise)
                    next_paths.append(path + [(nb, rel_type)])
            paths = next_paths
            if not paths:
                break
        return paths


class Neo4jGraph:
    """Thin live-DB backend with the same interface as LocalJsonGraph.
    Only built if you actually want to query live Neo4j instead of a
    static JSON snapshot -- not required for the offline agent demo."""

    def __init__(self, uri, user, password, database="neo4j", insecure_tls=False):
        from neo4j import GraphDatabase
        if insecure_tls:
            from neo4j import TrustAll
            uri = uri.replace("neo4j+s://", "neo4j://").replace("bolt+s://", "bolt://")
            self.driver = GraphDatabase.driver(uri, auth=(user, password), encrypted=True,
                                                trusted_certificates=TrustAll())
        else:
            self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database

    def _run(self, cypher, **params):
        with self.driver.session(database=self.database) as s:
            return [r.data() for r in s.run(cypher, **params)]

    def find_nodes(self, name_contains=None, label=None, doc_id=None):
        label_clause = f":`{label}`" if label else ""
        where = []
        params = {}
        if name_contains:
            where.append("toLower(n.name) CONTAINS toLower($name_contains)")
            params["name_contains"] = name_contains
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self._run(f"MATCH (n{label_clause}) {clause} RETURN n", **params)
        return [r["n"] for r in rows]

    def get_node(self, node_id):
        rows = self._run("MATCH (n {id: $id}) RETURN n", id=node_id)
        return rows[0]["n"] if rows else None

    def neighbors(self, node_id, rel_type=None, direction="any"):
        rel = f":`{rel_type}`" if rel_type else ""
        results = []
        if direction in ("out", "any"):
            for r in self._run(f"MATCH (a {{id:$id}})-[e{rel}]->(b) RETURN type(e) AS t, b", id=node_id):
                results.append((r["t"], "out", r["b"]))
        if direction in ("in", "any"):
            for r in self._run(f"MATCH (a {{id:$id}})<-[e{rel}]-(b) RETURN type(e) AS t, b", id=node_id):
                results.append((r["t"], "in", r["b"]))
        return results

    def multi_hop(self, start_id, hop_specs):
        start = self.get_node(start_id)
        if not start:
            return []
        paths = [[(start, None)]]
        for spec in hop_specs:
            next_paths = []
            for path in paths:
                visited = {n["id"] for n, _ in path}
                last_node = path[-1][0]
                for rel_type, _, nb in self.neighbors(last_node["id"], spec.get("rel"), spec.get("direction", "any")):
                    if nb["id"] in visited:
                        continue
                    next_paths.append(path + [(nb, rel_type)])
            paths = next_paths
            if not paths:
                break
        return paths

    def close(self):
        self.driver.close()


def default_graph_path():
    """Prefer the fuller ground-truth graph if present; fall back to the
    auto-extracted one. Either works -- see README for the coverage difference."""
    gt = ROOT / "data/synthetic/ground_truth_graph.json"
    ex = ROOT / "data/extracted/resolved_graph.json"
    return gt if gt.exists() else ex
