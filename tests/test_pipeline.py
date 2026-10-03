#!/usr/bin/env python3
"""Lightweight test suite (stdlib only -- pytest isn't installed in this
sandbox). Run:  python tests/test_pipeline.py
Exits non-zero on failure, so it also works as a CI check.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import extract  # noqa: E402
import resolve  # noqa: E402
import load_neo4j  # noqa: E402

failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


# --- extract.py -------------------------------------------------------
cfg, clause_catalog, vocab = extract.load_config()
doc_id, nodes, edges = extract.process_doc(ROOT / "data/synthetic/SYN-DEV-001_deviation.md", cfg, clause_catalog, vocab)
check("extract: finds the Deviation entity", "DEV-2026-031" in nodes)
check("extract: finds the Equipment entity by its canonical tag", "EQP-TP-03" in nodes)
check("extract: equipment picked up an alias mentioned in this doc",
      "Compression Line 3 press" in set(nodes["EQP-TP-03"].get("aliases", [])))
check("extract: finds a 21 CFR clause mention", any(n["label"] == "RegulationClause" for n in nodes.values()))
check("extract: produced at least one relation", len(edges) > 0)
check("extract: every node carries provenance", all(n.get("sources") for n in nodes.values()))

with tempfile.TemporaryDirectory() as tmp:
    out = Path(tmp) / "g.json"
    r = run([sys.executable, str(ROOT / "scripts/extract.py"), str(ROOT / "data/synthetic"), "--out", str(out)])
    check("extract.py CLI exits 0", r.returncode == 0)
    check("extract.py CLI wrote a file", out.exists())
    g = json.loads(out.read_text())
    check("extract.py CLI graph has all 5 docs represented",
          {s["doc_id"] for n in g["nodes"] for s in n["sources"]} >= {
              "SYN-BR-001", "SYN-DEV-001", "SYN-DEV-002", "SYN-CAPA-001", "SYN-CAPA-002"})
    check("extract: merging across ALL docs collects every known equipment alias",
          {"Press #3", "Compression Line 3 press", "Tablet Press 3"} <=
          set(next(n for n in g["nodes"] if n["id"] == "EQP-TP-03").get("aliases", [])))

# --- resolve.py ---------------------------------------------------------
sample = {
    "nodes": [
        {"label": "Equipment", "id": "EQP-A", "name": "HPLC-02", "aliases": ["LC-02"], "sources": [{"doc_id": "X"}]},
        {"label": "Equipment", "id": "EQP-B", "name": "LC-02", "sources": [{"doc_id": "Y"}]},
        {"label": "CAPA", "id": "CAP-A", "name": "CAPA-2026-011", "sources": [{"doc_id": "X"}]},
        {"label": "CAPA", "id": "CAP-B", "name": "CAPA-2026-019", "sources": [{"doc_id": "Y"}]},
    ],
    "edges": [
        {"type": "INVOLVES_EQUIPMENT", "from": {"label": "Deviation", "id": "DEV-1"},
         "to": {"label": "Equipment", "id": "EQP-B"}, "sources": [{"doc_id": "Y"}]},
    ],
}
resolved, report = resolve.resolve(sample, threshold=0.88,
                                    normalize_steps=["lower", "strip_punct", "collapse_space"],
                                    fuzzy_labels={"Equipment", "Site", "Product", "Inspector"})
check("resolve: merges true alias duplicate (LC-02 -> HPLC-02)", len(resolved["nodes"]) == 3)
check("resolve: does NOT merge lexically-similar-but-distinct CAPA ids",
      {"CAP-A", "CAP-B"} <= {n["id"] for n in resolved["nodes"]})
check("resolve: edge endpoint remapped to surviving node",
      resolved["edges"][0]["to"]["id"] in {"EQP-A"})

# --- load_neo4j.py (statement building only; no live DB in this sandbox) -
schema = json.loads((ROOT / "ontology/ontology.schema.json").read_text())
labels, rels = load_neo4j.allowed_types(schema)
ground_truth = json.loads((ROOT / "data/synthetic/ground_truth_graph.json").read_text())
statements, problems = load_neo4j.build_statements(ground_truth, labels, rels)
check("load_neo4j: builds one statement per node+edge with 0 skipped",
      len(problems) == 0 and len(statements) == len(ground_truth["nodes"]) + len(ground_truth["edges"]))
bad_graph = {"nodes": [{"label": "NotARealLabel", "id": "X-1", "name": "n", "sources": [{"doc_id": "d"}]}], "edges": []}
_, bad_problems = load_neo4j.build_statements(bad_graph, labels, rels)
check("load_neo4j: rejects an unknown label instead of crashing", len(bad_problems) == 1)

# --- ontology round-trip: ground truth still validates end to end --------
r = run([sys.executable, str(ROOT / "scripts/validate.py"), str(ROOT / "data/synthetic/ground_truth_graph.json")])
check("validate.py: ground truth graph still passes", r.returncode == 0)

print(f"\n{len(failures)} failing check(s)" if failures else "\nAll checks passed.")
sys.exit(1 if failures else 0)
