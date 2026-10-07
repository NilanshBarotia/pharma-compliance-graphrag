#!/usr/bin/env python3
"""Hybrid retrieval + multi-step investigation agent for the pharma
compliance knowledge graph. This is Version 1 / Point 3.

Pipeline (4 nodes, run in sequence, state threaded through a dict -- the
exact same node functions plug into a real LangGraph StateGraph when the
`langgraph` package is installed; see build_graph() below):

  1. retrieve_chunks   -- vector (TF-IDF) search over data/vector_store
  2. find_seed_entities -- regex-detect entity IDs in the question AND in
                            the top retrieved chunks (reuses extract.py's
                            own patterns -- one config, two consumers),
                            then resolves them to graph node ids
  3. graph_investigate  -- for each seed node, walks the hop chain
                            configured for its label in
                            config/agent_config.json (the brief's
                            "deviation -> equipment -> past CAPAs ->
                            clause" chain, made declarative)
  4. synthesize_answer  -- combines both kinds of evidence into a single
                            answer where EVERY sentence is tagged with
                            its source: [doc:ID#locator] for a text chunk,
                            [graph:NODE_ID] for a graph fact. This is the
                            citation grounding the brief asks for.

Usage:
  python scripts/agent.py "Which CAPAs trace back to 21 CFR 211.68(a)?"
  python scripts/agent.py "What happened with DEV-2026-031?" --graph-source neo4j --uri ... --password ... --insecure-tls
  python scripts/agent.py "..." --llm            # optional cosmetic rewording pass (see note below)
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from vector_store import VectorStore          # noqa: E402
from graph_client import LocalJsonGraph, Neo4jGraph, default_graph_path  # noqa: E402
import extract as extract_mod                  # noqa: E402  (reuse its regex patterns, not its CLI)


def load_agent_config():
    return json.loads((ROOT / "config/agent_config.json").read_text())


# ---------------------------------------------------------------- node 1 --
def retrieve_chunks(state, vs, k):
    hits = vs.search(state["question"], k=k)
    state["chunks"] = hits
    return state


# ---------------------------------------------------------------- node 2 --
def find_seed_entities(state, graph, extraction_cfg, max_seeds):
    """Looks for entity-ID-shaped strings (e.g. 'DEV-2026-031', '21 CFR
    211.68(a)') in the question itself, then in the retrieved chunks, and
    resolves each to a real graph node by matching name/aliases. This
    reuses extract.py's own regex patterns, so the agent recognizes
    exactly the same ID formats the extraction pipeline does."""
    def candidates_from(text):
        found = []
        for pat in extraction_cfg["entity_patterns"]:
            for m in re.finditer(pat["regex"], text, re.I):
                found.append((pat["label"], pat["name_from"].format(*m.groups())))
        return found

    # Entities named directly IN THE QUESTION take priority over entities
    # that merely happen to appear somewhere in a retrieved chunk -- e.g.
    # for "which CAPAs trace back to clause X", X should seed the graph
    # walk, not some unrelated SOP mentioned in the same chunk as X.
    question_candidates = candidates_from(state["question"])
    ordered_candidates = [(c, "question") for c in question_candidates]
    ordered_candidates += [(c, "retrieved text") for c in
                           dict.fromkeys(cand for chunk in state["chunks"] for cand in candidates_from(chunk["text"]))
                           if c not in question_candidates]

    seeds = []
    seen_ids = set()
    for (label, name), origin in ordered_candidates:
        if len(seeds) >= max_seeds:
            break
        for node in graph.find_nodes(name_contains=name, label=label):
            if node["id"] not in seen_ids:
                # copy: tag where the seed came from without mutating the graph's own node dict
                seeds.append({**node, "_origin": origin})
                seen_ids.add(node["id"])
    state["seed_entities"] = seeds[:max_seeds]
    return state


# ---------------------------------------------------------------- node 3 --
def _as_chain_list(spec):
    """config value for a label is either one chain (a list of hop dicts --
    the original format) or several chains (a list of lists of hop dicts)."""
    if spec and isinstance(spec[0], dict):
        return [spec]
    return spec or []


def graph_investigate(state, graph, chains):
    findings = []
    for seed in state["seed_entities"]:
        for hops in _as_chain_list(chains.get(seed["label"])):
            for path in graph.multi_hop(seed["id"], hops):
                findings.append(path)
    state["graph_findings"] = findings
    return state


# ---------------------------------------------------------------- node 4 --
def _node_label(n):
    return n.get("name") or n.get("id")


def synthesize_answer(state):
    lines = []
    if state["seed_entities"]:
        names = ", ".join(f"{s['label']} {_node_label(s)} [graph:{s['id']}]" for s in state["seed_entities"])
        lines.append(f"Identified in the graph: {names}.")
    else:
        lines.append("No recognized entity ID was found in the question or top text matches; "
                      "answer is based on text search only.")

    seen_paths = set()
    for path in state["graph_findings"]:
        key = tuple((step[0]["id"], step[1]) for step in path)
        if key in seen_paths or len(path) < 2:
            continue
        seen_paths.add(key)
        hops_desc = []
        for node, edge_type, hop_dir in path[1:]:
            arrow = f"--{edge_type}-->" if hop_dir != "in" else f"<--{edge_type}--"
            hops_desc.append(f"{arrow} {node['label']} {_node_label(node)} [graph:{node['id']}]")
        start_node = path[0][0]
        lines.append(f"{start_node['label']} {_node_label(start_node)} [graph:{start_node['id']}] " + " ".join(hops_desc))

    if state["chunks"]:
        lines.append("Supporting text evidence:")
        for c in state["chunks"]:
            snippet = c["text"].replace("\n", " ")[:160]
            lines.append(f"  - ({c['score']:.2f}) [doc:{c['doc_id']}#{c['locator']}] {snippet}...")

    state["answer"] = "\n".join(lines)
    return state


# ------------------------------------------------------- optional LLM pass --
LLM_SYSTEM_PROMPT = (
    "You will be given a question and a set of ALREADY-VERIFIED, cited findings "
    "from a knowledge graph and document search. Rewrite the findings into a "
    "clear, well-organized prose answer. Rules: (1) you MUST keep every "
    "[graph:...] and [doc:...] citation tag attached to the claim it supports; "
    "(2) you MUST NOT add any fact, number, date, or relationship that is not "
    "already present in the findings given to you; (3) if the findings are "
    "incomplete, say so rather than filling the gap from general knowledge."
)


def llm_reword(question, grounded_answer):
    """Optional cosmetic pass. Not used by default -- the grounded, templated
    answer from synthesize_answer() is already a complete, correctly-cited
    answer. Requires `pip install anthropic` and ANTHROPIC_API_KEY set."""
    try:
        import anthropic
    except ImportError:
        return grounded_answer + "\n\n[--llm requested but the `anthropic` package isn't installed; showing the grounded answer as-is.]"
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=600,
        system=LLM_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Question: {question}\n\nFindings:\n{grounded_answer}"}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


# --------------------------------------------------------------- runner ---
def build_and_run(question, vs, graph, agent_cfg, extraction_cfg):
    """Runs the 4 nodes in sequence. If `langgraph` is installed, this same
    set of functions is wired into a real langgraph.graph.StateGraph
    instead (see try/except below) -- the node logic itself never
    changes, only who calls it."""
    state = {"question": question}
    state = retrieve_chunks(state, vs, agent_cfg["vector_top_k"])
    state = find_seed_entities(state, graph, extraction_cfg, agent_cfg["max_seed_entities"])
    state = graph_investigate(state, graph, agent_cfg["investigation_chains"])
    state = synthesize_answer(state)
    return state


def try_build_langgraph(vs, graph, agent_cfg, extraction_cfg):
    """Returns a compiled LangGraph app if `langgraph` is installed, else
    None (caller falls back to build_and_run's plain sequential runner)."""
    try:
        from langgraph.graph import StateGraph, END
        from typing import TypedDict, List, Dict, Any

        class AgentState(TypedDict, total=False):
            question: str
            chunks: List[Dict[str, Any]]
            seed_entities: List[Dict[str, Any]]
            graph_findings: List[Any]
            answer: str

        g = StateGraph(AgentState)
        g.add_node("retrieve_chunks", lambda s: retrieve_chunks(s, vs, agent_cfg["vector_top_k"]))
        g.add_node("find_seed_entities", lambda s: find_seed_entities(s, graph, extraction_cfg, agent_cfg["max_seed_entities"]))
        g.add_node("graph_investigate", lambda s: graph_investigate(s, graph, agent_cfg["investigation_chains"]))
        g.add_node("synthesize_answer", synthesize_answer)
        g.set_entry_point("retrieve_chunks")
        g.add_edge("retrieve_chunks", "find_seed_entities")
        g.add_edge("find_seed_entities", "graph_investigate")
        g.add_edge("graph_investigate", "synthesize_answer")
        g.add_edge("synthesize_answer", END)
        return g.compile()
    except ImportError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--vector-store", default=str(ROOT / "data/vector_store"))
    ap.add_argument("--graph-source", choices=["json", "neo4j"], default="json")
    ap.add_argument("--graph-path", default=None, help="for --graph-source json (default: auto-picked)")
    ap.add_argument("--uri", default=None)
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default=None)
    ap.add_argument("--database", default="neo4j")
    ap.add_argument("--insecure-tls", action="store_true")
    ap.add_argument("--llm", action="store_true", help="optional cosmetic LLM rewording pass (see llm_reword())")
    args = ap.parse_args()

    vs = VectorStore.load(args.vector_store)
    if args.graph_source == "neo4j":
        graph = Neo4jGraph(args.uri, args.user, args.password, args.database, args.insecure_tls)
    else:
        graph = LocalJsonGraph(args.graph_path or default_graph_path())

    agent_cfg = load_agent_config()
    extraction_cfg = json.loads((ROOT / "config/extraction_patterns.json").read_text())

    app = try_build_langgraph(vs, graph, agent_cfg, extraction_cfg)
    if app is not None:
        print("[agent] running via installed `langgraph` StateGraph")
        state = app.invoke({"question": args.question})
    else:
        print("[agent] `langgraph` not installed -- running the equivalent sequential pipeline "
              "(identical node logic; `pip install langgraph` to use the real StateGraph runner)")
        state = build_and_run(args.question, vs, graph, agent_cfg, extraction_cfg)

    answer = state["answer"]
    if args.llm:
        answer = llm_reword(args.question, answer)

    print("\n=== ANSWER ===")
    print(answer)


if __name__ == "__main__":
    main()
