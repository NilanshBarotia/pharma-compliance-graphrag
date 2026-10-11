#!/usr/bin/env python3
"""Offline tests for Version 1 / Point 3 (hybrid retrieval + agent).
No Neo4j, no internet, no API key needed -- everything here runs against
the local TF-IDF vector store and the local JSON graph.
Run: python tests/test_agent_pipeline.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from vector_store import VectorStore          # noqa: E402
from graph_client import LocalJsonGraph        # noqa: E402
import agent                                   # noqa: E402

failures = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


# --- vector store ---------------------------------------------------------
vs_dir = ROOT / "data/vector_store"
check("vector store exists (run scripts/chunk_embed.py first)", vs_dir.exists())
if vs_dir.exists():
    vs = VectorStore.load(vs_dir)
    hits = vs.search("audit trail review not completed", k=3)
    check("vector search returns results", len(hits) > 0)
    check("top vector hit is the deviation about audit trails", hits[0]["doc_id"] == "SYN-DEV-002")

# --- graph client ----------------------------------------------------------
graph = LocalJsonGraph(ROOT / "data/synthetic/ground_truth_graph.json")
node = graph.get_node("REG-21CFR211.68(a)")
check("graph client resolves a known node id", node is not None and node["label"] == "RegulationClause")
nbrs = graph.neighbors("REG-21CFR211.68(a)", rel_type="RELATES_TO_CLAUSE", direction="in")
check("graph client finds the deviation relating to this clause",
      any(n["id"] == "DEV-0001" for _, _, n in nbrs))

paths = graph.multi_hop("REG-21CFR211.68(a)",
                         [{"rel": "RELATES_TO_CLAUSE", "direction": "in", "label": "Deviation"},
                          {"rel": "ADDRESSES", "direction": "in", "label": "CAPA"}])
check("multi_hop finds the CAPA that traces back to this clause",
      any(path[-1][0]["id"] == "CAP-0001" for path in paths))

cyclic_paths = graph.multi_hop("DEV-0001",
                                [{"rel": "INVOLVES_EQUIPMENT", "direction": "out"},
                                 {"rel": "INVOLVES_EQUIPMENT", "direction": "in"}])
check("multi_hop does not revisit the start node (cycle avoidance)",
      all(path[-1][0]["id"] != "DEV-0001" for path in cyclic_paths))

# --- end-to-end agent (the brief's example question) ------------------------
agent_cfg = agent.load_agent_config()
import json
extraction_cfg = json.loads((ROOT / "config/extraction_patterns.json").read_text())
state = agent.build_and_run("Which CAPAs trace back to 21 CFR 211.68(a)?", vs, graph, agent_cfg, extraction_cfg)

check("agent identifies the clause named in the question as a seed entity",
      any(s["id"] == "REG-21CFR211.68(a)" for s in state["seed_entities"]))
check("agent's graph walk reaches CAPA-2026-011",
      any(path[-1][0]["id"] == "CAP-0001" for path in state["graph_findings"]))
check("final answer cites the CAPA with a [graph:...] tag", "[graph:CAP-0001]" in state["answer"])
check("final answer includes at least one [doc:...] citation", "[doc:" in state["answer"])

# --- CLI smoke test ----------------------------------------------------------
r = subprocess.run([sys.executable, str(ROOT / "scripts/agent.py"),
                     "Which CAPAs trace back to 21 CFR 211.68(a)?"], capture_output=True, text=True)
check("agent.py CLI exits 0", r.returncode == 0)
check("agent.py CLI prints an ANSWER section", "=== ANSWER ===" in r.stdout)
check("agent.py CLI output cites CAP-0001", "[graph:CAP-0001]" in r.stdout)

print(f"\n{len(failures)} failing check(s)" if failures else "\nAll checks passed.")
sys.exit(1 if failures else 0)
