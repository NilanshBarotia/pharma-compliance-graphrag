#!/usr/bin/env python3
"""Generic vector store: fit, save, load, search. Works with ANY list of
(chunk_id, text, metadata) records -- nothing pharma-specific here.

Default backend is TF-IDF (scikit-learn): works fully offline, no API key,
no internet. This is a deliberate choice for this project: it lets the
whole retrieval pipeline run and be graded/demoed without any paid API.
Swap to a real embedding model later by implementing another `_fit`/
`_embed_query` pair and selecting it via config/chunking_rules.json's
"embedding.backend" -- the rest of the pipeline (save/load/search/CLI)
does not change.

Usage as a library:
    from vector_store import VectorStore
    vs = VectorStore()
    vs.fit(chunks)                      # chunks: list of dicts with 'id','text',...
    vs.save("data/vector_store")
    vs2 = VectorStore.load("data/vector_store")
    vs2.search("audit trail review", k=5)
"""
import json
import pickle
from pathlib import Path

import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


class VectorStore:
    def __init__(self, tfidf_kwargs=None):
        self.tfidf_kwargs = tfidf_kwargs or {"max_features": 4096, "ngram_range": (1, 2), "stop_words": "english"}
        self.vectorizer = None
        self.matrix = None       # (n_chunks, n_features) sparse
        self.chunks = []         # list of metadata dicts, same order as matrix rows

    def fit(self, chunks):
        """chunks: list of dicts, each must have 'id' and 'text'; any other
        keys are kept as metadata and returned verbatim on search."""
        self.chunks = chunks
        texts = [c["text"] for c in chunks]
        self.vectorizer = TfidfVectorizer(**self.tfidf_kwargs)
        self.matrix = self.vectorizer.fit_transform(texts)
        return self

    def search(self, query, k=5):
        if self.vectorizer is None:
            raise RuntimeError("VectorStore not fitted/loaded yet")
        q_vec = self.vectorizer.transform([query])
        sims = cosine_similarity(q_vec, self.matrix)[0]
        top_idx = np.argsort(-sims)[:k]
        return [{"score": float(sims[i]), **self.chunks[i]} for i in top_idx if sims[i] > 0]

    def save(self, directory):
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "vectorizer.pkl", "wb") as f:
            pickle.dump(self.vectorizer, f)
        sparse.save_npz(d / "vectors.npz", self.matrix)
        with open(d / "chunks.jsonl", "w", encoding="utf-8") as f:
            for c in self.chunks:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")

    @classmethod
    def load(cls, directory):
        d = Path(directory)
        vs = cls()
        with open(d / "vectorizer.pkl", "rb") as f:
            vs.vectorizer = pickle.load(f)
        vs.matrix = sparse.load_npz(d / "vectors.npz")
        vs.chunks = [json.loads(line) for line in open(d / "chunks.jsonl", encoding="utf-8")]
        return vs
