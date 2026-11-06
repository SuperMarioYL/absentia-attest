"""m2 — N×M hybrid scoring.

Two independent channels score **every** chunk against **every** variant:

* lexical — sqlite FTS5 with ``tokenize='trigram'`` (unicode61 turns a run of CJK
  into a single token and matches nothing), plus a documented ``LIKE`` fallback
  for 2-character CJK terms, which trigram cannot index;
* semantic — cosine similarity against an OpenAI-compatible embedding endpoint
  running on the operator's own machine (Ollama, llama.cpp). No torch, no
  hosted API.

Both produce a number in [0, 1], so the same τ is meaningful for either one, and
they are combined disjunctively (``max``): one channel firing strongly is
evidence, and the ledger records which one fired. Coverage is still 100 % —
every chunk is scored, so "absent" means "all N×M comparisons below τ", never
"the model did not mention it".

The lexical score is IDF-weighted term coverage rather than raw BM25. BM25
magnitudes drift with corpus size and cannot share a threshold with a cosine,
which would make τ corpus-dependent and the attestation incomparable across
archives. The rule is written down in docs/attestation-format.md.
"""

from __future__ import annotations

import hashlib
import math
import operator
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .ingest import canonical_json, pack_vector
from .variants import EMPTY_TERMS, Variant, normalize_claim, segment

SCORE_DECIMALS = 6

CJK_RANGE = r"\u3400-\u4dbf\u4e00-\u9fff"
_CJK_RUN = re.compile(rf"[{CJK_RANGE}]+")
# Function words that separate searchable terms in Chinese claims.
_PARTICLES = "的了过是和与及或"
_MIN_TERM_CHARS = 2
_TRIGRAM_MIN_CHARS = 3

# The single most discriminative term is what makes a claim specific. If it is
# missing, the remaining overlap is only half evidence — otherwise a claim about
# "Flock 摄像头" would score as if Flock had been found.
TOP_TERM_MISSING_GATE = 0.5

MATRIX_DIGEST_PREFIX = "absentia-matrix/1"
DEFAULT_EMBED_BATCH = 32


class EmbeddingFailure(Exception):
    """Embeddings were requested but could not be produced."""


@dataclass(frozen=True)
class ScanConfig:
    tau: float
    embedding_model: str | None = None
    endpoint: str | None = None
    timeout: float = 60.0


@dataclass(frozen=True)
class ScanOutcome:
    chunks: int
    variants: tuple[Variant, ...]
    per_variant: tuple[dict, ...]
    comparisons: int
    top1_score: float
    top1_chunk: str | None
    top1_variant: str | None
    matrix_sha256: str
    lexical_only: bool
    embedding_model: str | None
    embedding_error: str | None


# --------------------------------------------------------------------------- #
# term extraction
# --------------------------------------------------------------------------- #


def extract_terms(text: str) -> list[str]:
    """Search terms for one variant, in first-appearance order."""
    text = normalize_claim(text)
    terms: list[str] = []
    for token in re.findall(rf"[{CJK_RANGE}]+|[A-Za-z0-9][A-Za-z0-9._+-]*", text):
        if _CJK_RUN.fullmatch(token):
            for fragment in re.split(f"[{_PARTICLES}]", token):
                if fragment:
                    terms.extend(segment(fragment))
        else:
            terms.append(token)
    ordered: list[str] = []
    seen: set[str] = set()
    for term in terms:
        term = term.strip()
        if len(term) < _MIN_TERM_CHARS or term in EMPTY_TERMS or term in seen:
            continue
        seen.add(term)
        ordered.append(term)
    return ordered


def _like_escape(value: str) -> str:
    return value.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")


# --------------------------------------------------------------------------- #
# lexical channel
# --------------------------------------------------------------------------- #


def _match_term(connection: sqlite3.Connection, term: str) -> set[str]:
    if len(term) >= _TRIGRAM_MIN_CHARS:
        phrase = '"' + term.replace('"', '""') + '"'
        rows = connection.execute(
            "SELECT key FROM chunks_fts WHERE chunks_fts MATCH ?", (phrase,)
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT key FROM chunks WHERE text_norm LIKE ? ESCAPE '\\'",
            (f"%{_like_escape(term)}%",),
        ).fetchall()
    return {row[0] for row in rows}


@dataclass(frozen=True)
class LexicalScores:
    scores: dict[str, float]
    path: str  # fts_trigram | like_fallback | none
    terms: tuple[str, ...]


def lexical_scores(
    connection: sqlite3.Connection, text: str, total_chunks: int
) -> LexicalScores:
    terms = extract_terms(text)
    if not terms or total_chunks == 0:
        return LexicalScores({}, "none", tuple(terms))

    hits: dict[str, set[str]] = {}
    has_fts = False
    has_like = False
    for term in terms:
        key_set = _match_term(connection, term)
        hits[term] = key_set
        if key_set:
            if len(term) >= _TRIGRAM_MIN_CHARS:
                has_fts = True
            else:
                has_like = True

    # IDF from the archive itself: a rare term is worth more than a common one,
    # and a term the frame has never seen (df = 0) is worth the most — which is
    # exactly why its absence must keep the score down.
    idf = {term: math.log(1.0 + total_chunks / (1.0 + len(hits[term]))) for term in terms}
    denominator = sum(idf.values()) or 1.0
    top_term = max(terms, key=lambda term: (idf[term], len(term), term))

    matched_idf: dict[str, float] = {}
    for term, key_set in hits.items():
        for key in key_set:
            matched_idf[key] = matched_idf.get(key, 0.0) + idf[term]

    top_keys = hits[top_term]
    scores = {}
    for key, value in matched_idf.items():
        gate = 1.0 if key in top_keys else TOP_TERM_MISSING_GATE
        scores[key] = round(value / denominator * gate, SCORE_DECIMALS)
    path = "fts_trigram" if has_fts else ("like_fallback" if has_like else "none")
    return LexicalScores(scores, path, tuple(terms))


# --------------------------------------------------------------------------- #
# semantic channel
# --------------------------------------------------------------------------- #


def request_embeddings(
    texts: Sequence[str],
    *,
    endpoint: str,
    model: str,
    timeout: float = 60.0,
    batch_size: int = DEFAULT_EMBED_BATCH,
) -> list[list[float]]:
    """Embeddings from a local OpenAI-compatible endpoint (Ollama / llama.cpp)."""
    import httpx

    url = endpoint.rstrip("/") + "/embeddings"
    vectors: list[list[float]] = []
    with httpx.Client(timeout=timeout) as client:
        for start in range(0, len(texts), batch_size):
            batch = list(texts[start : start + batch_size])
            try:
                response = client.post(url, json={"model": model, "input": batch})
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                raise EmbeddingFailure(type(exc).__name__) from exc
            data = payload.get("data")
            if not isinstance(data, list) or len(data) != len(batch):
                raise EmbeddingFailure("invalid_response")
            ordered: list[list[float] | None] = [None] * len(batch)
            for position, item in enumerate(data):
                index = item.get("index", position)
                vector = item.get("embedding")
                if not isinstance(vector, list) or not vector:
                    raise EmbeddingFailure("invalid_response")
                if not 0 <= index < len(batch):
                    raise EmbeddingFailure("invalid_response")
                ordered[index] = vector
            if any(vector is None for vector in ordered):
                raise EmbeddingFailure("invalid_response")
            vectors.extend(vector for vector in ordered if vector is not None)
    return vectors


def _unit(vector: Sequence[float]) -> list[float]:
    """Unit-normalise once at ingest time so scoring is a plain dot product.

    A zero vector (an endpoint may return one for an empty string) carries no
    signal; it stays zero rather than failing the whole semantic channel.
    """
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        return [0.0] * len(vector)
    return [value / norm for value in vector]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return 0.0
    dot = sum(map(operator.mul, left, right))
    return max(0.0, min(1.0, dot))


# --------------------------------------------------------------------------- #
# the N×M pass
# --------------------------------------------------------------------------- #


def _load_chunks(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = connection.execute("SELECT key, text FROM chunks ORDER BY key").fetchall()
    return [(row[0], row[1]) for row in rows]


def run_scan(
    db_path: str | Path,
    variants: Sequence[Variant],
    config: ScanConfig,
    *,
    chunk_texts: Sequence[tuple[str, str]] | None = None,
) -> ScanOutcome:
    """Score every (chunk, variant) pair and digest the matrix.

    ``chunk_texts`` lets the caller reuse an in-memory chunk list; when omitted
    the texts are read back from the rebuilt sqlite index. Either way the pass
    runs in chunk-key order, so the digest does not depend on the caller.
    """
    connection = sqlite3.connect(db_path)
    try:
        chunks = list(chunk_texts) if chunk_texts is not None else _load_chunks(connection)
        chunks.sort(key=lambda item: item[0])
        total_chunks = len(chunks)
        chunk_keys = [key for key, _ in chunks]

        lexical_only = True
        embedding_model: str | None = None
        embedding_error: str | None = None
        chunk_vectors: dict[str, list[float]] = {}
        variant_vectors: list[list[float]] = []

        if config.endpoint and total_chunks:
            try:
                raw_chunk_vectors = request_embeddings(
                    [text for _, text in chunks],
                    endpoint=config.endpoint,
                    model=config.embedding_model or "bge-m3",
                    timeout=config.timeout,
                )
                raw_variant_vectors = request_embeddings(
                    [variant.text for variant in variants],
                    endpoint=config.endpoint,
                    model=config.embedding_model or "bge-m3",
                    timeout=config.timeout,
                )
                for key, vector in zip(chunk_keys, raw_chunk_vectors):
                    chunk_vectors[key] = _unit(vector)
                variant_vectors = [_unit(vector) for vector in raw_variant_vectors]
                lexical_only = False
                embedding_model = config.embedding_model or "bge-m3"
                connection.executemany(
                    "INSERT OR REPLACE INTO embeddings(key, dim, vector) VALUES (?,?,?)",
                    [
                        (key, len(vector), pack_vector(vector))
                        for key, vector in chunk_vectors.items()
                    ],
                )
                connection.commit()
            except EmbeddingFailure as exc:
                # Never crash, never claim a semantic bound we did not compute.
                lexical_only = True
                embedding_error = str(exc) or "failed"
                chunk_vectors = {}
                variant_vectors = []

        digest = hashlib.sha256()
        digest.update(MATRIX_DIGEST_PREFIX.encode("utf-8"))
        digest.update(b"\n")

        per_variant: list[dict] = []
        comparisons = 0
        top1_score = 0.0
        top1_chunk: str | None = None
        top1_variant: str | None = None

        for variant_index, variant in enumerate(variants):
            lexical = lexical_scores(connection, variant.text, total_chunks)
            semantic: dict[str, float] = {}
            if variant_vectors:
                vector = variant_vectors[variant_index]
                semantic = {
                    key: round(_cosine(vector, chunk_vectors[key]), SCORE_DECIMALS)
                    for key in chunk_keys
                }

            best_score = 0.0
            best_chunk: str | None = None
            above_tau = 0
            for key in chunk_keys:
                lex = lexical.scores.get(key, 0.0)
                sem = semantic.get(key, 0.0)
                score = max(lex, sem)
                comparisons += 1
                digest.update(
                    f"{variant_index}\t{key}\t{lex:.{SCORE_DECIMALS}f}\t{sem:.{SCORE_DECIMALS}f}\n".encode(
                        "utf-8"
                    )
                )
                if score >= config.tau:
                    above_tau += 1
                if score > best_score or (score == best_score and best_chunk is None):
                    best_score = score
                    best_chunk = key

            fired_by = "none"
            if best_chunk is not None:
                lex_best = lexical.scores.get(best_chunk, 0.0)
                sem_best = semantic.get(best_chunk, 0.0)
                if lex_best and sem_best:
                    fired_by = "both"
                elif lex_best:
                    fired_by = "lexical"
                elif sem_best:
                    fired_by = "semantic"

            per_variant.append(
                {
                    "variant": variant.text,
                    "chunks_scored": total_chunks,
                    "above_tau": above_tau,
                    "top1_score": round(best_score, SCORE_DECIMALS),
                    "top1_chunk": best_chunk,
                    "lexical_path": lexical.path,
                    "fired_by": fired_by,
                }
            )

            if best_chunk is not None and best_score > top1_score:
                top1_score = round(best_score, SCORE_DECIMALS)
                top1_chunk = best_chunk
                top1_variant = variant.text

        if top1_chunk is None and total_chunks:
            # Every score was zero for every variant: still report a chunk so the
            # ledger can cite what was ranked first.
            top1_chunk = chunk_keys[0]
        if top1_variant is None and variants:
            top1_variant = variants[0].text

        return ScanOutcome(
            chunks=total_chunks,
            variants=tuple(variants),
            per_variant=tuple(per_variant),
            comparisons=comparisons,
            top1_score=round(top1_score, SCORE_DECIMALS),
            top1_chunk=top1_chunk,
            top1_variant=top1_variant,
            matrix_sha256=digest.hexdigest(),
            lexical_only=lexical_only,
            embedding_model=embedding_model,
            embedding_error=embedding_error,
        )
    finally:
        connection.close()


def matrix_payload(outcome: ScanOutcome) -> dict:
    """The hashed description of the matrix pass."""
    return {
        "chunks": outcome.chunks,
        "variants": len(outcome.variants),
        "comparisons": outcome.comparisons,
        "sha256": outcome.matrix_sha256,
        "scoring": {
            "lexical": "fts5_trigram_idf_term_coverage",
            "semantic": "cosine",
            "combination": "max",
        },
    }


def matrix_canonical(outcome: ScanOutcome) -> str:
    return canonical_json(matrix_payload(outcome))


__all__ = [
    "CJK_RANGE",
    "EmbeddingFailure",
    "LexicalScores",
    "MATRIX_DIGEST_PREFIX",
    "SCORE_DECIMALS",
    "ScanConfig",
    "ScanOutcome",
    "extract_terms",
    "lexical_scores",
    "matrix_canonical",
    "matrix_payload",
    "request_embeddings",
    "run_scan",
]
