#!/usr/bin/env python3
"""Runs the demo questions in docs/example_questions.json through the agent,
CHECKS each answer against its hand-derived expectations, and writes the
results to docs/SAMPLE_QA.md (the README's sample Q&A section).

Three independent checks per question:
  1. expectation sanity -- every hand-written expected path is a real chain of
     edges in the ground-truth graph (so the expectations themselves are facts,
     not agent output echoed back)
  2. trace         -- each expected node-id sequence appears among the agent's
     traversed paths; no forbidden node appears in any path that starts from an
     entity named in the question
  3. grounding     -- expected source docs appear in the retrieved text evidence,
     and every [graph:...]/[doc:...] tag in the answer resolves to a real
     node/chunk

Usage:
  python scripts/run_examples.py                  # run + verify, print summary
  python scripts/run_examples.py --write          # also (re)write docs/SAMPLE_QA.md
  python scripts/run_examples.py --graph-source neo4j --uri ... --password ... --database ...
Exit code is non-zero if any check fails.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from service import AgentService, format_path, GRAPH_SOURCES   # noqa: E402


def edge_exists(gt, a, b):
    return any((e["from"]["id"], e["to"]["id"]) in ((a, b), (b, a)) for e in gt["edges"])


def verify(q, result, gt):
    problems = []

    # 1. expectation sanity (against the ground-truth file, not the agent)
    for seq in q["expect_paths"]:
        for a, b in zip(seq, seq[1:]):
            if not edge_exists(gt, a, b):
                problems.append(f"expectation error: no edge between {a} and {b} in ground truth")

    # 2. trace
    got = {tuple(h["id"] for h in p["hops"]) for p in result["paths"]}
    for seq in q["expect_paths"]:
        if tuple(seq) not in got:
            problems.append(f"missing expected path {' -> '.join(seq)}")
    q_seeds = {s["id"] for s in result["seeds"] if s["origin"] == "question"}
    for p in result["paths"]:
        if p["start"] in q_seeds:
            bad = [h["id"] for h in p["hops"] if h["id"] in set(q.get("forbid_nodes", []))]
            if bad:
                problems.append(f"forbidden node(s) {bad} on a path from a question entity")

    # 3. grounding
    docs = {c["doc_id"] for c in result["chunks"]}
    for d in q.get("expect_docs", []):
        if d not in docs:
            problems.append(f"expected source doc {d} not among retrieved evidence")
    for c in result["citations"]:
        if not c["resolved"]:
            problems.append(f"citation {c['kind']}:{c['ref']} does not resolve to a source")
    if not result["citations"]:
        problems.append("answer has no citations")
    return problems


def md_for(q, result, problems):
    q_seeds = {s["id"] for s in result["seeds"] if s["origin"] == "question"}
    lines = [f"### {q['id']}. {q['question']}", "",
             f"*Reasoning chain:* `{q['hops']}`  ", f"*Expected:* {q['expected_answer']}", "",
             "**Agent trace** (paths starting from entities named in the question):", "", "```"]
    shown = [p for p in result["paths"] if p["start"] in q_seeds]
    lines += [format_path(p) for p in shown] or ["(no graph path found)"]
    lines += ["```", "", "**Supporting text evidence:**", ""]
    for c in result["chunks"][:3]:
        snippet = c["text"].replace("\n", " ")
        snippet = snippet[:150] + ("..." if len(snippet) > 150 else "")
        lines.append(f"- `[doc:{c['doc_id']}#{c['locator']}]` (score {c['score']:.2f}) {snippet}")
    lines += ["", f"**Verdict:** {'PASS - trace matches the hand-derived expectation' if not problems else 'FAIL - ' + '; '.join(problems)}", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(ROOT / "docs/example_questions.json"))
    ap.add_argument("--graph-source", default="ground_truth", help="ground_truth | extracted | neo4j | path.json")
    ap.add_argument("--uri"); ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    ap.add_argument("--database", default="neo4j"); ap.add_argument("--insecure-tls", action="store_true")
    ap.add_argument("--write", action="store_true", help="write docs/SAMPLE_QA.md")
    args = ap.parse_args()

    neo = dict(uri=args.uri, user=args.user, password=args.password,
               database=args.database, insecure_tls=args.insecure_tls) if args.graph_source == "neo4j" else None
    svc = AgentService(graph_source=args.graph_source, neo4j=neo)
    gt = json.loads(GRAPH_SOURCES["ground_truth"].read_text())
    questions = json.loads(Path(args.questions).read_text())["questions"]

    blocks, failed = [], 0
    for q in questions:
        result = svc.ask(q["question"])
        problems = verify(q, result, gt)
        failed += bool(problems)
        print(f"{'PASS' if not problems else 'FAIL'}  {q['id']}  {q['question']}")
        for p in problems:
            print(f"        - {p}")
        blocks.append(md_for(q, result, problems))

    print(f"\n{len(questions) - failed}/{len(questions)} example questions traced correctly")

    if args.write:
        header = ("# Sample questions and traced answers\n\n"
                  "Generated by `python scripts/run_examples.py --write` against the "
                  f"`{args.graph_source}` graph. Every trace below was checked automatically against "
                  "hand-derived expectations (`docs/example_questions.json`) - the verdict line is the "
                  "result of that check, not a claim. All entities are fictional synthetic data; "
                  "regulatory clauses are real public citations.\n\n"
                  "Arrow direction is the true stored edge direction: `A --REL--> B` means the edge "
                  "points from A to B, `A <--REL-- B` means it points from B to A.\n\n")
        out = ROOT / "docs/SAMPLE_QA.md"
        out.write_text(header + "\n".join(blocks), encoding="utf-8")
        print(f"wrote {out.relative_to(ROOT)}")
    svc.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
