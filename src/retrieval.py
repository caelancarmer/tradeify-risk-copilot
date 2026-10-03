"""
retrieval.py -- Hybrid retrieval: BM25 + TF-IDF vector, fused with RRF.

Why this instead of "just embeddings":
  - Regulatory/financial text is keyword-sensitive ("daily loss limit" must
    match the chunk that literally defines it). Pure vector search drifts to
    semantically-near-but-wrong chunks. BM25 anchors precision; vectors add
    recall for paraphrases. RRF fuses them without score calibration.
  - Pure-Python implementation: zero heavy dependencies, runs on CPU.
  - Production path: swap TFIDFScorer for an embedding model (BGE-M3 /
    multilingual-e5) behind the same VectorScorer interface; swap the in-memory
    index for pgvector + tsvector. The fusion and eval code does not change.

Run: python3 src/retrieval.py  (smoke test)
"""

import json
import math
import os
import re
from collections import Counter

STOPWORDS = set("""
a an the and or of to in on for with is are was were be been being by as at
from that this these those it its into over under after before between what
when where which who whom how why do does did can could should would will
my your his her their our i you he she we they me him us them s t d ll re ve
m
""".split())


def tokenize(text: str) -> list[str]:
    # NOTE (2026-10-03): light plural stemming was tried here and REVERTED.
    # The "Porter-lite" stemmer mangled keyword-bearing singulars
    # (basis->basi, news->new, pauses->paus, touches->touche, releases->releas),
    # breaking exact regulatory keyword matches and dropping hybrid hit@1
    # 0.833 -> 0.800, MRR 0.903 -> 0.886 (30-question harness). A corrected
    # conservative plural stemmer only tied no-stemming (0.867/0.925) and the
    # generic -s rule still corrupted non-plurals, so no stemming is kept.
    return [t for t in re.findall(r"[a-z0-9]+", text.lower())
            if t not in STOPWORDS and len(t) > 1]


class BM25Scorer:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.N = len(docs)
        self.doc_len = [len(d) for d in docs]
        self.avgdl = sum(self.doc_len) / max(1, self.N)
        df: Counter = Counter()
        for d in docs:
            for t in set(d):
                df[t] += 1
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        out = [0.0] * self.N
        qtf = Counter(query)
        for i, doc in enumerate(self.docs):
            tf = Counter(doc)
            dl = self.doc_len[i]
            s = 0.0
            for t, q in qtf.items():
                if t not in tf:
                    continue
                num = tf[t] * (self.k1 + 1)
                den = tf[t] + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                s += self.idf.get(t, 0.0) * num / den
            out[i] = s
        return out


class TFIDFScorer:
    """Sparse TF-IDF vectors + cosine. Stands in for dense embeddings offline."""

    def __init__(self, docs: list[list[str]]):
        self.N = len(docs)
        df: Counter = Counter()
        for d in docs:
            for t in set(d):
                df[t] += 1
        self.idf = {t: math.log((self.N + 1) / (n + 1)) + 1.0 for t, n in df.items()}
        self.doc_vecs: list[dict[str, float]] = []
        self.doc_norm: list[float] = []
        for d in docs:
            tf = Counter(d)
            vec = {t: (1 + math.log(c)) * self.idf[t] for t, c in tf.items() if t in self.idf}
            n = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            self.doc_vecs.append(vec)
            self.doc_norm.append(n)

    def scores(self, query: list[str]) -> list[float]:
        tf = Counter(query)
        qvec = {t: (1 + math.log(c)) * self.idf[t] for t, c in tf.items() if t in self.idf}
        qn = math.sqrt(sum(v * v for v in qvec.values())) or 1.0
        out = []
        for vec, dn in zip(self.doc_vecs, self.doc_norm):
            dot = sum(qvec[t] * vec[t] for t in qvec if t in vec)
            out.append(dot / (qn * dn))
        return out


def rrf_fuse(rankings: list[list[int]], k: int = 60) -> list[float]:
    """Reciprocal Rank Fusion over index-rankings. Returns fused scores per doc."""
    n = len(rankings[0])
    fused = [0.0] * n
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            fused[idx] += 1.0 / (k + rank + 1)
    return fused


def _argsort_desc(scores: list[float]) -> list[int]:
    return sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)


class HybridRetriever:
    def __init__(self, chunks: list[dict]):
        self.chunks = chunks
        docs = [tokenize(c["title"] + " " + c["text"] + " " + c["text"]) for c in chunks]
        self.bm25 = BM25Scorer(docs)
        self.vector = TFIDFScorer(docs)
        # NOTE (2026-10-02): a third RRF voter on titles alone was tried and
        # REVERTED — the harness showed a regression (hybrid hit@1 0.833->0.767).
        # With a 9-chunk corpus the spiky title signal overfits; kept as a
        # documented negative result. Revisit with a larger corpus.

    def retrieve(self, query: str, k: int = 5, method: str = "hybrid") -> list[dict]:
        qtok = tokenize(query)
        b = self.bm25.scores(qtok)
        v = self.vector.scores(qtok)
        if method == "bm25":
            order, scores = _argsort_desc(b), b
        elif method == "vector":
            order, scores = _argsort_desc(v), v
        elif method == "hybrid":
            fused = rrf_fuse([_argsort_desc(b), _argsort_desc(v)])
            order, scores = _argsort_desc(fused), fused
        else:
            raise ValueError(f"unknown method {method}")
        return [{"id": self.chunks[i]["id"], "title": self.chunks[i]["title"],
                 "text": self.chunks[i]["text"], "rule_id": self.chunks[i]["rule_id"],
                 "score": round(scores[i], 4)}
                for i in order[:k]]

    def grade(self, query: str, chunks: list[dict], min_score: float = 0.0) -> bool:
        """Relevance gate for the agentic loop: True if top chunk looks relevant."""
        if not chunks:
            return False
        top_terms = set(tokenize(query))
        top_text = set(tokenize(chunks[0]["title"] + " " + chunks[0]["text"][:400]))
        overlap = len(top_terms & top_text)
        return overlap >= max(2, len(top_terms) // 3)


def load_corpus(path: str | None = None) -> list[dict]:
    path = path or os.path.join(os.path.dirname(__file__), "..", "data", "rulebook_chunks.json")
    with open(path) as f:
        return json.load(f)


if __name__ == "__main__":
    r = HybridRetriever(load_corpus())
    for q in ["What happens if I hit the daily loss limit on Growth?",
              "When does the trailing drawdown lock?",
              "Can I hold positions overnight?"]:
        print(f"\nQ: {q}")
        for h in r.retrieve(q, k=3):
            print(f"  {h['id']:24s} score={h['score']}")
