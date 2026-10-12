#!/usr/bin/env python3
"""Surface-agnostic service layer for the compliance GraphRAG agent (Point 4).

Both user interfaces (Streamlit `app.py`, terminal `scripts/cli.py`) and the
example runner (`scripts/run_examples.py`) call exactly this module, so they
can never disagree about what the agent said.

It does NOT re-implement any agent logic. It runs agent.py's own node
functions and converts the resulting state into a plain, JSON-serialisable
dict (`ask()` result) with:

  question, engine, graph_source
  steps        -- per-node trace: name, seconds, one-line summary
  seeds        -- entities the walk started from, tagged question|retrieved text
  paths        -- every traversed multi-hop path, as ordered hops with the
                  edge's TRUE direction (so arrows can be drawn correctly)
  chunks       -- vector hits: doc_id, locator, score, text
  citations    -- every [graph:...] / [doc:...] tag, resolved to its node/chunk
  answer       -- the agent's own cited text answer (unchanged)
"""
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))

import agent as agent_mod                                   # noqa: E402
from vector_store import VectorStore                        # noqa: E402
from graph_client import LocalJsonGraph, Neo4jGraph         # noqa: E402

GRAPH_SOURCES = {
    "ground_truth": ROOT / "data/synthetic/ground_truth_graph.json",
    "extracted": ROOT / "data/extracted/resolved_graph.json",
}
CITE_RE = re.compile(r"\[(graph|doc):([^\]]+)\]")


def _jsonable_node(n):
    """Node dict -> small display dict (drops internal keys, keeps props)."""
    skip = {"sources", "aliases", "_origin", "label", "id", "name"}
    return {
        "id": n.get("id"),
        "label": n.get("label"),
        "name": n.get("name") or n.get("id"),
        "aliases": n.get("aliases", []),
        "properties": {k: v for k, v in n.items() if k not in skip and not k.startswith("_")},
        "sources": n.get("sources", []),
    }


class AgentService:
    def __init__(self, graph_source="ground_truth", vector_dir=None,
                 neo4j=None, top_k=None, max_seeds=None):
        """graph_source: 'ground_truth' | 'extracted' | 'neo4j' | path to a graph.json.
        neo4j: dict(uri, user, password, database, insecure_tls) when graph_source == 'neo4j'."""
        self.vs = VectorStore.load(vector_dir or (ROOT / "data/vector_store"))
        self.agent_cfg = agent_mod.load_agent_config()
        if top_k:
            self.agent_cfg["vector_top_k"] = top_k
        if max_seeds:
            self.agent_cfg["max_seed_entities"] = max_seeds
        self.extraction_cfg = json.loads((ROOT / "config/extraction_patterns.json").read_text())

        self.graph_source = graph_source
        if graph_source == "neo4j":
            n = neo4j or {}
            self.graph = Neo4jGraph(n["uri"], n.get("user", "neo4j"), n["password"],
                                    n.get("database", "neo4j"), n.get("insecure_tls", False))
        else:
            path = GRAPH_SOURCES.get(graph_source, Path(graph_source))
            self.graph = LocalJsonGraph(path)
        self._lg_app = agent_mod.try_build_langgraph(self.vs, self.graph, self.agent_cfg, self.extraction_cfg)

    # ------------------------------------------------------------ helpers --
    def graph_stats(self):
        g = self.graph
        if not hasattr(g, "nodes"):
            return {"nodes": None, "edges": None, "labels": {}}
        labels = {}
        for n in g.nodes.values():
            labels[n["label"]] = labels.get(n["label"], 0) + 1
        return {"nodes": len(g.nodes), "edges": len(g.edges), "labels": labels}

    # --------------------------------------------------------------- run --
    def _run_nodes(self, question):
        """Runs the agent's four node functions one by one (timed).
        If `langgraph` is installed, streams the real compiled StateGraph
        instead and times each node as its update arrives."""
        steps = []
        cfg = self.agent_cfg
        if self._lg_app is not None:
            try:
                state, t0 = {"question": question}, time.perf_counter()
                for update in self._lg_app.stream({"question": question}, stream_mode="updates"):
                    for node_name, upd in update.items():
                        now = time.perf_counter()
                        state.update(upd or {})
                        steps.append({"name": node_name, "seconds": now - t0})
                        t0 = now
                if "answer" in state:
                    return state, steps, "langgraph StateGraph"
            except Exception:           # fall back to the equivalent sequential run
                steps = []

        state = {"question": question}
        plan = [
            ("retrieve_chunks", lambda s: agent_mod.retrieve_chunks(s, self.vs, cfg["vector_top_k"])),
            ("find_seed_entities", lambda s: agent_mod.find_seed_entities(
                s, self.graph, self.extraction_cfg, cfg["max_seed_entities"])),
            ("graph_investigate", lambda s: agent_mod.graph_investigate(s, self.graph, cfg["investigation_chains"])),
            ("synthesize_answer", agent_mod.synthesize_answer),
        ]
        for name, fn in plan:
            t0 = time.perf_counter()
            state = fn(state)
            steps.append({"name": name, "seconds": time.perf_counter() - t0})
        return state, steps, "sequential (same node functions)"

    def ask(self, question, use_llm=False):
        question = question.strip()
        state, steps, engine = self._run_nodes(question)

        # ---- seeds
        seeds = []
        for s in state.get("seed_entities", []):
            d = _jsonable_node(s)
            d["origin"] = s.get("_origin", "question")
            seeds.append(d)

        # ---- paths (dedupe on node ids + edge types, drop single-node paths)
        paths, seen = [], set()
        for raw in state.get("graph_findings", []):
            if len(raw) < 2:
                continue
            key = tuple((step[0]["id"], step[1]) for step in raw)
            if key in seen:
                continue
            seen.add(key)
            hops = []
            for node, rel, direction in raw:
                hop = _jsonable_node(node)
                hop.update({"via": rel, "direction": direction})
                hops.append(hop)
            paths.append({"start": hops[0]["id"], "hops": hops})

        # ---- chunks
        chunks = [{"doc_id": c["doc_id"], "locator": c.get("locator", ""), "score": round(c["score"], 4),
                   "text": c["text"], "id": c.get("id")} for c in state.get("chunks", [])]

        answer = state["answer"]
        if use_llm:
            answer = agent_mod.llm_reword(question, answer)

        # ---- citations: resolve every tag that appears in the answer
        citations, seen_c = [], set()
        by_doc = {(c["doc_id"], c["locator"]): c for c in chunks}
        for kind, ref in CITE_RE.findall(answer):
            if (kind, ref) in seen_c:
                continue
            seen_c.add((kind, ref))
            if kind == "graph":
                node = self.graph.get_node(ref)
                citations.append({"kind": "graph", "ref": ref,
                                  "resolved": bool(node), "node": _jsonable_node(node) if node else None})
            else:
                doc_id, _, locator = ref.partition("#")
                chunk = by_doc.get((doc_id, locator))
                citations.append({"kind": "doc", "ref": ref, "resolved": bool(chunk),
                                  "doc_id": doc_id, "locator": locator,
                                  "text": chunk["text"] if chunk else None})

        # ---- one-line summary per step (for the trace view)
        summaries = {
            "retrieve_chunks": f"{len(chunks)} text chunks retrieved (top-k vector search)",
            "find_seed_entities": f"{len(seeds)} seed entit{'y' if len(seeds) == 1 else 'ies'}: "
                                  + (", ".join(s["id"] for s in seeds) or "none found"),
            "graph_investigate": f"{len(paths)} multi-hop path{'s' if len(paths) != 1 else ''} traversed",
            "synthesize_answer": f"{len(citations)} citations attached, "
                                 f"{sum(c['resolved'] for c in citations)} resolved to a source",
        }
        for st in steps:
            st["summary"] = summaries.get(st["name"], "")

        return {"question": question, "engine": engine, "graph_source": self.graph_source,
                "steps": steps, "seeds": seeds, "paths": paths, "chunks": chunks,
                "citations": citations, "answer": answer}

    def close(self):
        if hasattr(self.graph, "close"):
            self.graph.close()


# ------------------------------------------------------------ rendering ----
def format_path(path, with_ids=True):
    """One readable line for a path, arrows following the real edge direction."""
    def label(h):
        base = f"{h['label']} {h['name']}"
        return f"{base} [graph:{h['id']}]" if with_ids else base
    out = [label(path["hops"][0])]
    for h in path["hops"][1:]:
        arrow = f"--{h['via']}-->" if h["direction"] != "in" else f"<--{h['via']}--"
        out.append(f"{arrow} {label(h)}")
    return " ".join(out)


def _dot_escape(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


NODE_COLORS = {
    "Deviation": "#F4B183", "CAPA": "#A9D18E", "Equipment": "#9DC3E6", "RegulationClause": "#D5A6E6",
    "SOP": "#FFE699", "Batch": "#C9C9C9", "Product": "#F8CBAD", "Site": "#B4C7E7", "Inspector": "#F7CAC9",
}


def to_dot(result, only_seed_paths=False):
    """Graphviz DOT for the sub-graph the agent traversed. Edges are drawn
    in their true stored direction (from -> to). Seeds get a bold outline."""
    seed_ids = {s["id"] for s in result["seeds"] if s["origin"] == "question"}
    all_seed_ids = {s["id"] for s in result["seeds"]}
    nodes, edges = {}, set()
    for p in result["paths"]:
        if only_seed_paths and p["start"] not in seed_ids:
            continue
        prev = None
        for h in p["hops"]:
            nodes[h["id"]] = h
            if prev is not None:
                a, b = (prev["id"], h["id"]) if h["direction"] != "in" else (h["id"], prev["id"])
                edges.add((a, b, h["via"]))
            prev = h
    lines = ["digraph G {", '  rankdir=LR; bgcolor="transparent";',
             '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10];',
             '  edge [fontname="Helvetica", fontsize=8, color="#666666"];']
    for nid, h in nodes.items():
        name = h["name"] if len(h["name"]) <= 38 else h["name"][:35] + "..."
        pen = ', penwidth=3, color="#C00000"' if nid in seed_ids else (', penwidth=2' if nid in all_seed_ids else "")
        lines.append(f'  "{_dot_escape(nid)}" [label="{_dot_escape(h["label"])}\\n{_dot_escape(name)}", '
                     f'fillcolor="{NODE_COLORS.get(h["label"], "#EEEEEE")}"{pen}];')
    for a, b, rel in sorted(edges):
        lines.append(f'  "{_dot_escape(a)}" -> "{_dot_escape(b)}" [label="{_dot_escape(rel)}"];')
    lines.append("}")
    return "\n".join(lines)
