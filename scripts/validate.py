#!/usr/bin/env python3
"""Validate a graph document against the ontology and the project's data.

Checks
  1. JSON Schema conformance (uses `jsonschema` if installed; otherwise a small
     built-in fallback that supports the keyword subset our schema uses).
  2. Referential integrity: unique node ids, edge endpoints exist and their
     labels match the endpoint node's label.
  3. Provenance: every sources[].doc_id exists in data/sources/sources.json
     (SRC-*) or in data/synthetic/*.md front matter (SYN-*).
  4. Coverage report: node labels / relationship types used.

Usage:  python scripts/validate.py [graph.json]
        (default: data/synthetic/ground_truth_graph.json)
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "ontology" / "ontology.schema.json"
DEFAULT_GRAPH = ROOT / "data" / "synthetic" / "ground_truth_graph.json"


# ---------- fallback JSON Schema validator (subset) ----------
def _resolve(ref, root):
    node = root
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def _check(inst, sch, root, path, errs):
    if "$ref" in sch:
        return _check(inst, _resolve(sch["$ref"], root), root, path, errs)
    if "oneOf" in sch:
        results = []
        for sub in sch["oneOf"]:
            e = []
            _check(inst, sub, root, path, e)
            results.append(e)
        ok = [r for r in results if not r]
        if len(ok) != 1:
            # report errors from the branch whose const discriminator matched, if any
            best = min(results, key=len)
            errs.append(f"{path}: matches {len(ok)} of {len(results)} oneOf branches; closest: {best[:2]}")
        return
    if "const" in sch and inst != sch["const"]:
        errs.append(f"{path}: expected const {sch['const']!r}, got {inst!r}")
    if "enum" in sch and inst not in sch["enum"]:
        errs.append(f"{path}: {inst!r} not in enum {sch['enum']}")
    t = sch.get("type")
    tmap = {"object": dict, "array": list, "string": str, "boolean": bool, "number": (int, float)}
    if t and not (isinstance(inst, tmap[t]) and not (t == "number" and isinstance(inst, bool))):
        errs.append(f"{path}: expected {t}")
        return
    if t == "string":
        if "pattern" in sch and not re.search(sch["pattern"], inst):
            errs.append(f"{path}: {inst!r} !~ {sch['pattern']}")
        if len(inst) < sch.get("minLength", 0):
            errs.append(f"{path}: too short")
    if t == "array":
        if len(inst) < sch.get("minItems", 0):
            errs.append(f"{path}: needs >= {sch['minItems']} items")
        if sch.get("uniqueItems") and len({json.dumps(i, sort_keys=True) for i in inst}) != len(inst):
            errs.append(f"{path}: items not unique")
        for i, item in enumerate(inst):
            if "items" in sch:
                _check(item, sch["items"], root, f"{path}[{i}]", errs)
    if t == "object":
        props = sch.get("properties", {})
        for r in sch.get("required", []):
            if r not in inst:
                errs.append(f"{path}: missing required '{r}'")
        if sch.get("additionalProperties") is False:
            for k in inst:
                if k not in props:
                    errs.append(f"{path}: unexpected property '{k}'")
        for k, v in inst.items():
            if k in props:
                _check(v, props[k], root, f"{path}.{k}", errs)


def schema_errors(graph, schema):
    try:
        import jsonschema
        v = jsonschema.Draft202012Validator(schema)
        return [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}" for e in v.iter_errors(graph)], "jsonschema"
    except ImportError:
        errs = []
        _check(graph, schema, schema, "$", errs)
        return errs, "built-in fallback"


# ---------- project checks ----------
def known_doc_ids():
    ids = {s["doc_id"] for s in json.loads((ROOT / "data/sources/sources.json").read_text())["sources"]}
    for md in (ROOT / "data/synthetic").glob("*.md"):
        m = re.search(r"^doc_id:\s*(\S+)", md.read_text(), re.M)
        if m:
            ids.add(m.group(1))
    return ids


def main(graph_path):
    graph = json.loads(Path(graph_path).read_text())
    schema = json.loads(SCHEMA.read_text())
    problems = []

    errs, engine = schema_errors(graph, schema)
    problems += [f"[schema] {e}" for e in errs]

    nodes = {}
    for n in graph["nodes"]:
        if n["id"] in nodes:
            problems.append(f"[integrity] duplicate node id {n['id']}")
        nodes[n["id"]] = n["label"]
    for e in graph["edges"]:
        for end in ("from", "to"):
            ref = e[end]
            if ref["id"] not in nodes:
                problems.append(f"[integrity] {e['type']}: {end} id {ref['id']} not found")
            elif nodes[ref["id"]] != ref["label"]:
                problems.append(f"[integrity] {e['type']}: {ref['id']} is {nodes[ref['id']]}, edge says {ref['label']}")

    known = known_doc_ids()
    for item in graph["nodes"] + graph["edges"]:
        for s in item.get("sources", []):
            if s["doc_id"] not in known:
                problems.append(f"[provenance] unknown doc_id {s['doc_id']}")

    labels = {n["label"] for n in graph["nodes"]}
    all_labels = {k[5:] for k in schema["$defs"] if k.startswith("node_")}
    all_rels = {k[5:] for k in schema["$defs"] if k.startswith("edge_")}
    used_rels = {e["type"] for e in graph["edges"]}

    print(f"schema engine : {engine}")
    print(f"nodes / edges : {len(graph['nodes'])} / {len(graph['edges'])}")
    print(f"labels covered: {len(labels)}/{len(all_labels)}  missing: {sorted(all_labels - labels) or 'none'}")
    print(f"rels covered  : {len(used_rels)}/{len(all_rels)}  missing: {sorted(all_rels - used_rels) or 'none'}")
    unverified = [n["id"] for n in graph["nodes"] if n.get("verification_status") == "unverified"]
    if unverified:
        print(f"note          : {len(unverified)} clause node(s) still 'unverified': {', '.join(unverified)}")
    if problems:
        print(f"\nFAILED with {len(problems)} problem(s):")
        for p in problems:
            print(" -", p)
        return 1
    print("\nOK: graph conforms to ontology, references resolve, provenance is known.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_GRAPH))
