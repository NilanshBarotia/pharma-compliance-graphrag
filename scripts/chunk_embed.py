#!/usr/bin/env python3
"""Chunk every document in data/synthetic/ and data/raw/ and embed the
chunks into a vector store (data/vector_store/ by default).

Generic / config-driven: chunking strategy per document type lives in
config/chunking_rules.json. To add a new document type, add a rule there
-- no code changes needed, as long as it fits one of the 3 strategies
already implemented (section / fixed / whole_section).

Every chunk keeps full provenance (doc_id + locator), which is what makes
citation grounding possible later in agent.py.

Usage:
  python scripts/chunk_embed.py
  python scripts/chunk_embed.py --out data/vector_store
"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "chunking_rules.json"
FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.M)


def load_config():
    return json.loads(CONFIG.read_text())


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


def chunk_by_section(doc_id, body, min_chars):
    """One chunk per '## Heading' block (markdown synthetic docs)."""
    chunks = []
    positions = [(m.start(), m.group(1).strip()) for m in HEADING_RE.finditer(body)]
    positions.append((len(body), None))
    # also capture the preamble before the first heading, if substantial
    if positions and positions[0][0] > 0:
        pre = body[:positions[0][0]].strip()
        if len(pre) >= min_chars:
            chunks.append({"locator": "preamble", "text": pre})
    for (start, heading), (end, _) in zip(positions, positions[1:]):
        text = body[start:end].strip()
        if len(text) >= min_chars:
            chunks.append({"locator": f"section:{heading.lower()}", "text": text})
    return chunks


def chunk_fixed(doc_id, body, chunk_chars, overlap_chars):
    chunks = []
    i = 0
    n = len(body)
    while i < n:
        end = min(i + chunk_chars, n)
        text = body[i:end].strip()
        if text:
            chunks.append({"locator": f"offset:{i}-{end}", "text": text})
        if end == n:
            break
        i = end - overlap_chars
    return chunks


def chunk_synthetic_docs(cfg, rules):
    records = []
    for path in sorted((ROOT / "data/synthetic").glob("*.md")):
        text = path.read_text()
        meta, body = read_front_matter(text)
        doc_id = meta.get("doc_id", path.stem)
        doc_type = meta.get("doc_type", "default")
        rule = rules.get(doc_type, rules["default"])
        if rule["strategy"] == "section":
            raw_chunks = chunk_by_section(doc_id, body, rule.get("min_chars", 40))
        else:
            raw_chunks = chunk_fixed(doc_id, body, rule.get("chunk_chars", 900), rule.get("overlap_chars", 150))
        for i, c in enumerate(raw_chunks):
            records.append({"id": f"{doc_id}::chunk{i}", "doc_id": doc_id, "doc_type": doc_type,
                            "locator": c["locator"], "text": c["text"], "synthetic": True})
    return records


def chunk_raw_sources(rules):
    """Reads whatever scripts/fetch_sources.py downloaded into data/raw/.
    Skips gracefully (prints a notice) if that directory is empty -- this
    keeps the pipeline runnable even before fetch_sources.py has been run."""
    records = []
    raw_dir = ROOT / "data/raw"
    sources = json.loads((ROOT / "data/sources/sources.json").read_text())["sources"]
    by_id = {s["doc_id"]: s for s in sources}

    for doc_dir in sorted(raw_dir.iterdir()) if raw_dir.exists() else []:
        if not doc_dir.is_dir():
            continue
        doc_id = doc_dir.name
        src = by_id.get(doc_id, {})
        doc_type = "regulation_section" if doc_id == "SRC-CFR-211" else \
                   "warning_letter" if "WL" in doc_id else "default"

        sections_file = doc_dir / "sections.jsonl"
        text_file = doc_dir / "text.txt"
        if sections_file.exists():
            rule = rules.get("regulation_section", rules["default"])
            for line in sections_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                s = json.loads(line)
                records.append({"id": f"{doc_id}::{s['section']}", "doc_id": doc_id, "doc_type": doc_type,
                                "locator": f"section:{s['section']}", "text": f"{s.get('heading','')}\n{s.get('text','')}".strip(),
                                "synthetic": False})
        elif text_file.exists():
            rule = rules.get(doc_type, rules["default"])
            body = text_file.read_text(encoding="utf-8", errors="ignore")
            raw_chunks = chunk_fixed(doc_id, body, rule.get("chunk_chars", 900), rule.get("overlap_chars", 150))
            for i, c in enumerate(raw_chunks):
                records.append({"id": f"{doc_id}::chunk{i}", "doc_id": doc_id, "doc_type": doc_type,
                                "locator": c["locator"], "text": c["text"], "synthetic": False})
    if not records:
        print("[chunk_embed] note: data/raw/ has no fetched text yet "
              "(run scripts/fetch_sources.py first to include real regulation/warning-letter text)")
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data/vector_store"))
    args = ap.parse_args()

    cfg = load_config()
    rules = cfg["rules"]

    records = chunk_synthetic_docs(cfg, rules) + chunk_raw_sources(rules)
    print(f"[chunk_embed] built {len(records)} chunks "
          f"({sum(r['synthetic'] for r in records)} from synthetic docs, "
          f"{sum(not r['synthetic'] for r in records)} from fetched real sources)")

    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from vector_store import VectorStore
    tfidf_kwargs = cfg["embedding"]["tfidf"]
    tfidf_kwargs = {**tfidf_kwargs, "ngram_range": tuple(tfidf_kwargs["ngram_range"])}
    vs = VectorStore(tfidf_kwargs=tfidf_kwargs).fit(records)
    vs.save(args.out)
    print(f"[chunk_embed] saved vector store to {args.out} "
          f"({vs.matrix.shape[0]} chunks x {vs.matrix.shape[1]} TF-IDF features)")


if __name__ == "__main__":
    main()
