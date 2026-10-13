#!/usr/bin/env python3
"""Terminal interface for the compliance GraphRAG agent (no auth, single user).

  python scripts/cli.py "Which CAPAs trace back to 21 CFR 211.68(a)?"   # one question
  python scripts/cli.py                                                   # interactive prompt
  python scripts/cli.py --examples                                        # list demo questions
  python scripts/cli.py --example Q3                                      # run demo question Q3
  python scripts/cli.py "..." --json                                      # machine-readable result
  python scripts/cli.py "..." --trace                                     # also show per-step trace
  python scripts/cli.py "..." --graph-source neo4j --uri neo4j+s://... --user ... --database ... --insecure-tls
      (Neo4j password is read from --password or $NEO4J_PASSWORD)

For the raw agent output (original format) use scripts/agent.py; this CLI
adds grouping, colour, trace and JSON on top of the same node functions.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from service import AgentService, format_path       # noqa: E402

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def load_examples():
    return json.loads((ROOT / "docs/example_questions.json").read_text())["questions"]


def render(result, show_trace=False, show_context=False):
    out = [c(f"Q: {result['question']}", "1")]
    out.append(c(f"[engine: {result['engine']} | graph: {result['graph_source']}]", "2"))

    if show_trace:
        out.append(c("\nAgent steps", "1;36"))
        for i, st in enumerate(result["steps"], 1):
            out.append(f"  {i}. {st['name']:<20} {st['seconds'] * 1000:6.1f} ms  {st.get('summary', '')}")

    q_seeds = [s for s in result["seeds"] if s["origin"] == "question"]
    ctx_seeds = [s for s in result["seeds"] if s["origin"] != "question"]
    out.append(c("\nEntities recognised in your question", "1;36"))
    if q_seeds:
        for s in q_seeds:
            out.append(f"  - {s['label']}: {s['name']}  {c('[graph:' + s['id'] + ']', '33')}")
    else:
        out.append("  (none - no entity ID in the question. The graph walk started from entities seen in the "
                   "retrieved text instead; treat it as a lead. Include an ID for a precise trace.)")

    q_ids = {s["id"] for s in q_seeds}
    main = [p for p in result["paths"] if p["start"] in q_ids]
    ctx = [p for p in result["paths"] if p["start"] not in q_ids]
    if not q_seeds:
        main, ctx = ctx, []
    out.append(c("\nGraph findings (multi-hop)", "1;36"))
    out += [f"  * {format_path(p)}" for p in main] or ["  (no graph path found)"]
    if ctx and show_context:
        out.append(c("\nRelated context (entities that appeared in retrieved text)", "1;36"))
        out += [c(f"  * {format_path(p)}", "2") for p in ctx]
    elif ctx:
        out.append(c(f"\n({len(ctx)} related-context paths from entities seen only in retrieved text "
                     "are hidden; use --context to show them)", "2"))

    out.append(c("\nSupporting text evidence", "1;36"))
    for ch in result["chunks"]:
        snippet = ch["text"].replace("\n", " ")
        snippet = snippet[:140] + ("..." if len(snippet) > 140 else "")
        out.append(f"  - ({ch['score']:.2f}) {c('[doc:' + ch['doc_id'] + '#' + ch['locator'] + ']', '33')} {snippet}")

    bad = [x for x in result["citations"] if not x["resolved"]]
    out.append(c(f"\nCitations: {len(result['citations'])} total, "
                 f"{len(result['citations']) - len(bad)} resolved to a source"
                 + (f", {len(bad)} UNRESOLVED" if bad else ""), "1;32" if not bad else "1;31"))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="?", help="omit for an interactive prompt")
    ap.add_argument("--examples", action="store_true", help="list the demo questions and exit")
    ap.add_argument("--example", metavar="ID", help="run a demo question by id (e.g. Q3)")
    ap.add_argument("--json", action="store_true", help="print the full structured result as JSON")
    ap.add_argument("--trace", action="store_true", help="show the per-step agent trace")
    ap.add_argument("--context", action="store_true", help="also show paths from entities seen only in retrieved text")
    ap.add_argument("--llm", action="store_true", help="optional cosmetic rewording (needs anthropic + ANTHROPIC_API_KEY)")
    ap.add_argument("--graph-source", default="ground_truth", help="ground_truth | extracted | neo4j | path.json")
    ap.add_argument("--uri"); ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    ap.add_argument("--database", default="neo4j"); ap.add_argument("--insecure-tls", action="store_true")
    args = ap.parse_args()

    examples = load_examples()
    if args.examples:
        for q in examples:
            print(f"{q['id']:>3}  {q['question']}")
        return

    question = args.question
    if args.example:
        match = [q for q in examples if q["id"].lower() == args.example.lower()]
        if not match:
            sys.exit(f"unknown example id {args.example!r}; run --examples to list them")
        question = match[0]["question"]

    if args.graph_source == "neo4j" and not (args.uri and args.password):
        sys.exit("--graph-source neo4j needs --uri and a password (--password or $NEO4J_PASSWORD)")
    neo = dict(uri=args.uri, user=args.user, password=args.password,
               database=args.database, insecure_tls=args.insecure_tls) if args.graph_source == "neo4j" else None
    svc = AgentService(graph_source=args.graph_source, neo4j=neo)

    def run(q):
        res = svc.ask(q, use_llm=args.llm)
        print(json.dumps(res, indent=2, ensure_ascii=False) if args.json else render(res, args.trace, args.context))

    try:
        if question:
            run(question)
        else:
            print("Pharma compliance GraphRAG - interactive. Type a question, 'examples', or 'quit'.")
            while True:
                try:
                    q = input(c("\n> ", "1;32")).strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not q:
                    continue
                if q.lower() in {"quit", "exit", "q"}:
                    break
                if q.lower() == "examples":
                    for ex in examples:
                        print(f"{ex['id']:>3}  {ex['question']}")
                    continue
                if q.upper() in {ex["id"] for ex in examples}:
                    q = next(ex["question"] for ex in examples if ex["id"] == q.upper())
                run(q)
    finally:
        svc.close()


if __name__ == "__main__":
    main()
