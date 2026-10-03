#!/usr/bin/env python3
"""Generic entity resolution over a graph document.

Works on ANY graph produced for this ontology (or any graph-document with
label/id/name/aliases/sources), independent of domain. Two nodes with the
SAME LABEL are merged if, after normalization, their name or any alias
matches exactly, or their names are fuzzy-similar above a threshold
(difflib ratio - no extra dependency required).

This is a second line of defense after extract.py's alias_patterns: it
catches variants nobody wrote a regex for (typos, minor rewordings)
without needing a domain-specific dictionary.

Usage:
  python scripts/resolve.py data/extracted/extracted_graph.json --out data/extracted/resolved_graph.json
  python scripts/resolve.py in.json --threshold 0.85 --report report.json
"""
import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "extraction_patterns.json"


def normalize(s, steps):
    s = s or ""
    if "lower" in steps:
        s = s.lower()
    if "strip_punct" in steps:
        s = re.sub(r"[^\w\s]", "", s)
    if "collapse_space" in steps:
        s = re.sub(r"\s+", " ", s).strip()
    return s


def all_keys(node, steps):
    """Every normalized string that could identify this node: name + aliases."""
    return {normalize(node["name"], steps)} | {normalize(a, steps) for a in node.get("aliases", [])}


def merge(canonical, dupe):
    """Fold `dupe` into `canonical`, keeping canonical's id/name, unioning the rest."""
    canonical["sources"] = canonical["sources"] + [s for s in dupe["sources"] if s not in canonical["sources"]]
    new_aliases = set(canonical.get("aliases", [])) | set(dupe.get("aliases", []))
    new_aliases.add(dupe["name"])
    new_aliases.discard(canonical["name"])
    if new_aliases:
        canonical["aliases"] = sorted(new_aliases)
    for k, v in dupe.items():
        if k in ("id", "name", "label", "sources", "aliases"):
            continue
        canonical.setdefault(k, v)
    return canonical


def resolve(graph, threshold, normalize_steps, fuzzy_labels):
    by_label = {}
    for n in graph["nodes"]:
        by_label.setdefault(n["label"], []).append(n)

    id_remap = {}
    kept_nodes = []
    report = []

    for label, nodes in by_label.items():
        clusters = []  # each: {"canon": node, "keys": set(...)}
        for n in nodes:
            keys = all_keys(n, normalize_steps)
            placed = False
            for c in clusters:
                if keys & c["keys"]:
                    c["canon"] = merge(c["canon"], n)
                    c["keys"] |= keys
                    id_remap[n["id"]] = c["canon"]["id"]
                    report.append({"label": label, "merged_id": n["id"], "into_id": c["canon"]["id"], "reason": "exact/alias match"})
                    placed = True
                    break
                best = max((SequenceMatcher(None, k1, k2).ratio() for k1 in keys for k2 in c["keys"]), default=0)
                if label in fuzzy_labels and best >= threshold:
                    c["canon"] = merge(c["canon"], n)
                    c["keys"] |= keys
                    id_remap[n["id"]] = c["canon"]["id"]
                    report.append({"label": label, "merged_id": n["id"], "into_id": c["canon"]["id"],
                                  "reason": f"fuzzy match ({best:.2f})"})
                    placed = True
                    break
            if not placed:
                clusters.append({"canon": dict(n), "keys": keys})
                id_remap[n["id"]] = n["id"]
        kept_nodes.extend(c["canon"] for c in clusters)

    seen_edges = set()
    kept_edges = []
    for e in graph["edges"]:
        frm = {"label": e["from"]["label"], "id": id_remap.get(e["from"]["id"], e["from"]["id"])}
        to = {"label": e["to"]["label"], "id": id_remap.get(e["to"]["id"], e["to"]["id"])}
        if frm["id"] == to["id"]:
            continue  # merge collapsed both ends onto the same node; drop self-loop
        key = (e["type"], frm["id"], to["id"])
        if key in seen_edges:
            continue
        seen_edges.add(key)
        e2 = dict(e)
        e2["from"], e2["to"] = frm, to
        kept_edges.append(e2)

    return {"nodes": kept_nodes, "edges": kept_edges}, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("graph_path")
    ap.add_argument("--out", default=None)
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text()).get("resolution", {}) if CONFIG.exists() else {}
    threshold = args.threshold if args.threshold is not None else cfg.get("fuzzy_threshold", 0.85)
    steps = cfg.get("normalize", ["lower", "strip_punct", "collapse_space"])
    fuzzy_labels = set(cfg.get("fuzzy_labels", []))

    graph = json.loads(Path(args.graph_path).read_text())
    before_n, before_e = len(graph["nodes"]), len(graph["edges"])
    resolved, report = resolve(graph, threshold, steps, fuzzy_labels)

    out = args.out or str(Path(args.graph_path).with_name("resolved_graph.json"))
    Path(out).write_text(json.dumps(resolved, indent=2))
    print(f"[resolve] nodes {before_n} -> {len(resolved['nodes'])}, edges {before_e} -> {len(resolved['edges'])}")
    print(f"[resolve] {len(report)} merge(s); threshold={threshold}")
    for r in report:
        print(f"  - {r['label']}: {r['merged_id']} -> {r['into_id']} ({r['reason']})")
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2))
    print(f"[resolve] wrote {out}")


if __name__ == "__main__":
    main()
