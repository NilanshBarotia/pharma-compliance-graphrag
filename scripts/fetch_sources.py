#!/usr/bin/env python3
"""Download the real public texts listed in data/sources/sources.json.

Outputs (git-ignored; see .gitignore):
  data/raw/<doc_id>/original.<ext>   the file exactly as downloaded
  data/raw/<doc_id>/text.txt         plain text (pdf/html sources)
  data/raw/SRC-CFR-211/sections.jsonl  one JSON object per CFR section
  data/sources/fetch_log.json        url, timestamp, sha256, bytes, status

Usage:
  python scripts/fetch_sources.py              # everything
  python scripts/fetch_sources.py --only SRC-CFR-211 SRC-EUGMP-A11
  python scripts/fetch_sources.py --parse-cfr-xml path/to/title-21.xml   # offline parse only

Requires network access, `requests`, `beautifulsoup4`, `pypdf` (see requirements.txt).
"""
import argparse
import datetime as dt
import hashlib
import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "data" / "sources" / "sources.json"
RAW = ROOT / "data" / "raw"
LOG = ROOT / "data" / "sources" / "fetch_log.json"
HEADERS = {"User-Agent": "pharma-graphrag-educational/0.1 (research project; contact via repo issues)"}
DELAY_S = 2.0  # be polite to public servers


def parse_cfr_xml(xml_bytes):
    """Return [{'section': '211.22', 'heading': ..., 'text': ..., 'paragraphs': [...]}, ...]."""
    root = ET.fromstring(xml_bytes)
    out = []
    for el in root.iter():
        if el.tag.upper().startswith("DIV") and el.attrib.get("TYPE") == "SECTION":
            head = next((h for h in el if h.tag.upper() == "HEAD"), None)
            heading = " ".join("".join(head.itertext()).split()) if head is not None else ""
            paras = [" ".join("".join(p.itertext()).split()) for p in el if p.tag.upper() == "P"]
            num = el.attrib.get("N", "").replace("§", "").strip()
            out.append({"section": num, "heading": heading, "paragraphs": paras, "text": "\n".join(paras)})
    return out


def _get(url, **kw):
    import requests
    r = requests.get(url, headers=HEADERS, timeout=60, **kw)
    r.raise_for_status()
    return r


def fetch_cfr(src, outdir):
    titles = _get("https://www.ecfr.gov/api/versioner/v1/titles.json").json()["titles"]
    date = next(t for t in titles if t["number"] == 21)["up_to_date_as_of"]
    r = _get(src["api_url_template"].format(date=date))
    (outdir / "original.xml").write_bytes(r.content)
    sections = parse_cfr_xml(r.content)
    with open(outdir / "sections.jsonl", "w") as f:
        for s in sections:
            s.update(doc_id=src["doc_id"], ecfr_as_of=date)
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    return r.content, f"{len(sections)} sections, eCFR as of {date}"


def fetch_pdf(src, outdir):
    from pypdf import PdfReader
    r = _get(src["url"])
    (outdir / "original.pdf").write_bytes(r.content)
    reader = PdfReader(str(outdir / "original.pdf"))
    text = "\n\n".join(f"[page {i+1}]\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages))
    (outdir / "text.txt").write_text(text)
    return r.content, f"{len(reader.pages)} pages"


# def fetch_html(src, outdir):
#     from bs4 import BeautifulSoup
#     r = _get(src["url"])
#     (outdir / "original.html").write_bytes(r.content)
#     soup = BeautifulSoup(r.content, "html.parser")
#     main = soup.find("article") or soup.find("main") or soup.body
#     for tag in main.find_all(["script", "style", "nav", "footer"]):
#         tag.decompose()
#     text = "\n".join(line.strip() for line in main.get_text("\n").splitlines() if line.strip())
#     (outdir / "text.txt").write_text(text)
#     return r.content, f"{len(text.split())} words"

def fetch_html(src, outdir):
    from bs4 import BeautifulSoup

    html_path = outdir / "original.html"
    r = type("R", (), {"content": html_path.read_bytes()})()

    soup = BeautifulSoup(r.content, "html.parser")
    main = soup.find("article") or soup.find("main") or soup.body
    for tag in main.find_all(["script", "style", "nav", "footer"]):
        tag.decompose()
    text = "\n".join(line.strip() for line in main.get_text("\n").splitlines() if line.strip())
    (outdir / "text.txt").write_text(text)

    return r.content, f"{len(text.split())} words"

# I manually downloaded the FDA files and so made a change to the above function

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="doc_ids to fetch")
    ap.add_argument("--parse-cfr-xml", help="parse a local eCFR XML file and print a summary (no network)")
    args = ap.parse_args()

    if args.parse_cfr_xml:
        secs = parse_cfr_xml(Path(args.parse_cfr_xml).read_bytes())
        print(f"{len(secs)} sections")
        for s in secs[:5]:
            print(" ", s["section"], "|", s["heading"])
        return

    sources = json.loads(INDEX.read_text())["sources"]
    log = json.loads(LOG.read_text()) if LOG.exists() else {}
    for src in sources:
        if args.only and src["doc_id"] not in args.only:
            continue
        outdir = RAW / src["doc_id"]
        outdir.mkdir(parents=True, exist_ok=True)
        entry = {"url": src["url"], "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        try:
            fn = fetch_cfr if src["doc_id"] == "SRC-CFR-211" else fetch_pdf if src["format"] == "pdf" else fetch_html
            content, info = fn(src, outdir)
            entry.update(status="ok", bytes=len(content), sha256=hashlib.sha256(content).hexdigest(), info=info)
            print(f"[ok]   {src['doc_id']}: {info}")
        except Exception as e:  # keep going; report at the end
            entry.update(status="error", error=f"{type(e).__name__}: {e}")
            print(f"[fail] {src['doc_id']}: {entry['error']}")
        log[src["doc_id"]] = entry
        LOG.write_text(json.dumps(log, indent=2))
        time.sleep(DELAY_S)


if __name__ == "__main__":
    main()