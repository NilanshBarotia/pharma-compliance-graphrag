#!/usr/bin/env python3
"""Generic entity + relation extraction, driven entirely by config.

Nothing pharma-specific lives in this file. To point it at a different
document set: edit config/extraction_patterns.json (regexes, id templates,
relation keywords) and config/vocab.json / config/clause_catalog.json
(property lookups). The ontology contract (ontology/ontology.schema.json)
stays the same.

Method (rule-based, explainable, no ML model required):
  1. entity mentions  = regex matches from `entity_patterns`, plus
                         `alias_patterns` that resolve to an existing
                         entity's canonical id without changing its name.
  2. relations         = "keyword-anchored window" matching: for every
                         relation_trigger, every keyword occurrence in the
                         text is a candidate anchor; the nearest preceding
                         mention of `from_label` and nearest following (or
                         preceding) mention of `to_label` within
                         `window_chars` are linked.
  3. structural post-processing: RegulationClause parenthetical clauses
     (e.g. 211.68(a)) get an automatic CHILD_OF edge to their base clause
     (211.68), and clause property lookup from config/clause_catalog.json.

Usage:
  python scripts/extract.py data/synthetic --out data/extracted/extracted_graph.json
"""
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "extraction_patterns.json"
CLAUSE_CATALOG = ROOT / "config" / "clause_catalog.json"
VOCAB = ROOT / "config" / "vocab.json"
WINDOW_CHARS = 400  # how far (in characters) a relation keyword may reach to find its two entities

FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.M)


def load_config():
    cfg = json.loads(CONFIG.read_text())
    clauses = json.loads(CLAUSE_CATALOG.read_text()) if CLAUSE_CATALOG.exists() else {"frameworks": {}}
    vocab = json.loads(VOCAB.read_text()) if VOCAB.exists() else {}
    return cfg, clauses, vocab


def read_front_matter(text):
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, text[m.end():]


def locator_for(pos, headings):
    """Which '## heading' section a character offset falls under, if any."""
    best = None
    for h_pos, h_text in headings:
        if h_pos <= pos:
            best = h_text
        else:
            break
    return best


def find_mentions(text, headings, entity_patterns):
    """Return list of dicts: label, id, name, span, locator, extra props found inline."""
    mentions = []
    for pat in entity_patterns:
        for m in re.finditer(pat["regex"], text, re.I):
            groups = m.groups()
            eid = pat["id_from"].format(*groups)
            name = pat["name_from"].format(*groups)
            mention = {
                "label": pat["label"], "id": eid, "name": name,
                "span": m.span(), "locator": locator_for(m.start(), headings),
                "props": {},
            }
            if pat.get("framework"):
                mention["props"]["framework"] = pat["framework"].format(*groups)
            title_re = pat.get("capture_title_regex")
            if title_re:
                tm = re.search(title_re.format(*groups), text)
                if tm:
                    mention["props"]["_title"] = tm.group(1).strip()
            mentions.append(mention)
    mentions.sort(key=lambda x: x["span"][0])
    return mentions


def nearest(mentions, pos, label, before=True):
    cands = [m for m in mentions if m["label"] == label and (m["span"][1] <= pos if before else m["span"][0] >= pos)]
    if not cands:
        return None
    return max(cands, key=lambda m: m["span"][1]) if before else min(cands, key=lambda m: m["span"][0])


def extract_relations(text, mentions, relation_triggers):
    edges = []
    lower = text.lower()
    for rel in relation_triggers:
        for kw in rel["keywords"]:
            for km in re.finditer(re.escape(kw), lower):
                lo, hi = max(0, km.start() - WINDOW_CHARS), min(len(text), km.end() + WINDOW_CHARS)
                window = [m for m in mentions if lo <= m["span"][0] <= hi]
                frm = nearest(window, km.start(), rel["from_label"], before=True) or nearest(window, km.start(), rel["from_label"], before=False)
                to = nearest(window, km.end(), rel["to_label"], before=False) or nearest(window, km.end(), rel["to_label"], before=True)
                if frm and to and frm["id"] != to["id"]:
                    edges.append({"type": rel["type"], "from_id": frm["id"], "from_label": frm["label"],
                                  "to_id": to["id"], "to_label": to["label"], "anchor": kw})
    return edges


def dedupe_edges(raw_edges, doc_id):
    seen = {}
    for e in raw_edges:
        key = (e["type"], e["from_id"], e["to_id"])
        if key not in seen:
            seen[key] = {"type": e["type"],
                         "from": {"label": e["from_label"], "id": e["from_id"]},
                         "to": {"label": e["to_label"], "id": e["to_id"]},
                         "sources": [{"doc_id": doc_id, "locator": f"anchor:{e['anchor']}"}]}
    return list(seen.values())


def clause_lookup(clause_catalog, framework, section):
    fw = clause_catalog.get("frameworks", {}).get(framework, {})
    return fw.get("sections", {}).get(section, {}), fw.get("doc_id")


def build_reg_props(mention, clause_catalog):
    props = {"framework": mention["props"].get("framework", "OTHER"), "citation": mention["name"]}
    m = re.search(r"(\d{1,2}),?\s*clause\s*(\d{1,2})", mention["name"], re.I) if "ANNEX" in props["framework"] else None
    if m:
        section = m.group(2)
    else:
        section = re.sub(r"^21 CFR ", "", mention["name"])
    entry, _ = clause_lookup(clause_catalog, props["framework"], section)
    props["title"] = entry.get("_title") or mention["props"].get("_title", "")
    props["jurisdiction"] = "EU" if "EU" in props["framework"] else "US"
    props["verification_status"] = "verified_against_source" if entry.get("verified") else "unverified"
    return props


def add_clause_hierarchy(nodes_by_id):
    """Auto-add CHILD_OF edges for parenthetical CFR clauses, e.g. 211.68(a) -> 211.68."""
    edges = []
    for nid, n in list(nodes_by_id.items()):
        if n["label"] != "RegulationClause":
            continue
        m = re.match(r"^(REG-21CFR-\d{3}\.\d+)\([a-z0-9]+\)$", nid)
        if m:
            parent_id = m.group(1)
            if parent_id not in nodes_by_id:
                parent_name = re.sub(r"\(.*\)$", "", n["name"]).strip()
                nodes_by_id[parent_id] = {
                    "label": "RegulationClause", "id": parent_id, "name": parent_name,
                    "sources": n["sources"], "synthetic": False,
                    "framework": n.get("framework", "OTHER"), "citation": parent_name,
                    "jurisdiction": n.get("jurisdiction", "US"),
                    "verification_status": "unverified",
                }
            edges.append({"type": "CHILD_OF", "from": {"label": "RegulationClause", "id": nid},
                          "to": {"label": "RegulationClause", "id": parent_id}, "sources": n["sources"]})
    return edges


def apply_alias_patterns(text, headings, alias_patterns, mentions_by_canon):
    """Find alternate surface forms (e.g. 'Press #3') and fold them in as aliases
    of an already-extracted canonical entity, without creating a duplicate node."""
    extra_aliases = defaultdict(set)
    extra_sources = []
    for pat in alias_patterns:
        for m in re.finditer(pat["regex"], text, re.I):
            canon_id = pat["canonical_id_from"].format(*m.groups())
            if canon_id in mentions_by_canon:
                extra_aliases[canon_id].add(m.group(0))
                extra_sources.append((canon_id, m.start()))
    return extra_aliases


def process_doc(path, cfg, clause_catalog, vocab):
    text = path.read_text()
    meta, body = read_front_matter(text)
    doc_id = meta.get(cfg["front_matter_doc_id_field"], path.stem)
    headings = [(m.start(), m.group(1).strip().lower()) for m in HEADING_RE.finditer(body)]

    mentions = find_mentions(body, headings, cfg["entity_patterns"])
    by_canon = {m["id"]: m for m in mentions}
    aliases = apply_alias_patterns(body, headings, cfg.get("alias_patterns", []), by_canon)

    nodes = {}
    for m in mentions:
        node = nodes.setdefault(m["id"], {
            "label": m["label"], "id": m["id"], "name": m["name"],
            "aliases": sorted(aliases.get(m["id"], set())),
            "synthetic": meta.get("synthetic", "").lower() == "true",
            "sources": [],
        })
        loc = f"section:{m['locator']}" if m["locator"] else "document"
        if not any(s["doc_id"] == doc_id and s["locator"] == loc for s in node["sources"]):
            node["sources"].append({"doc_id": doc_id, "locator": loc})
        if m["label"] == "RegulationClause":
            node.update(build_reg_props(m, clause_catalog))
        if m["label"] == "SOP":
            title = m["props"].get("_title")
            if title or "title" not in node:
                node["title"] = title or m["name"]  # fallback: no inline *Title* found near this mention yet
            prefix = m["id"].split("-")[1] if "-" in m["id"] else ""
            dept = vocab.get("sop_department_by_prefix", {}).get(prefix)
            if dept:
                node["department"] = dept
            node["sop_number"] = m["name"]
        if m["label"] == "Equipment":
            tag = m["id"]
            node["equipment_tag"] = tag
            etype = vocab.get("equipment_type_by_tag_prefix", {}).get(tag.split("-")[0])
            if etype:
                node["equipment_type"] = etype
        if m["label"] in ("Deviation", "CAPA"):
            node["title"] = node["name"]
            key = "deviation_number" if m["label"] == "Deviation" else "capa_number"
            node[key] = m["name"]
        if m["label"] == "Batch":
            node["batch_number"] = m["name"].replace("Lot ", "")
    for node in nodes.values():
        if not node["aliases"]:
            node.pop("aliases")
        if not node.get("synthetic"):
            node.pop("synthetic", None)

    raw_edges = extract_relations(body, mentions, cfg["relation_triggers"])
    edges = dedupe_edges(raw_edges, doc_id)
    return doc_id, nodes, edges


def merge_nodes(a, b):
    """Merge two mention-derived node dicts for the same id (e.g. seen in two docs)."""
    out = dict(a)
    out["sources"] = a["sources"] + [s for s in b["sources"] if s not in a["sources"]]
    out["aliases"] = sorted(set(a.get("aliases", [])) | set(b.get("aliases", [])))
    if not out["aliases"]:
        out.pop("aliases")
    for k, v in b.items():
        if k in ("sources", "aliases"):
            continue
        out.setdefault(k, v)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_dir", help="directory of .md documents with --- doc_id: ... --- front matter")
    ap.add_argument("--out", default=str(ROOT / "data/extracted/extracted_graph.json"))
    args = ap.parse_args()

    cfg, clause_catalog, vocab = load_config()
    all_nodes, all_edges = {}, []
    for path in sorted(Path(args.input_dir).glob("*.md")):
        doc_id, nodes, edges = process_doc(path, cfg, clause_catalog, vocab)
        for nid, n in nodes.items():
            all_nodes[nid] = merge_nodes(all_nodes[nid], n) if nid in all_nodes else n
        all_edges.extend(edges)
        print(f"[extract] {path.name} ({doc_id}): {len(nodes)} entity mentions, {len(edges)} relations")

    hierarchy_edges = add_clause_hierarchy(all_nodes)
    # dedupe hierarchy edges against existing
    existing = {(e["type"], e["from"]["id"], e["to"]["id"]) for e in all_edges}
    for e in hierarchy_edges:
        key = (e["type"], e["from"]["id"], e["to"]["id"])
        if key not in existing:
            all_edges.append(e)
            existing.add(key)

    graph = {"nodes": list(all_nodes.values()), "edges": all_edges}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(graph, indent=2))
    print(f"[extract] wrote {args.out}: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")


if __name__ == "__main__":
    main()
