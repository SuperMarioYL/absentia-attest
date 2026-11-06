"""m2 — 判定、摘要与 verify.

Two rules shape this module.

1. **The model is not in the decision path.** ``decide()`` reads numbers that
   every operator can recompute: the highest of the N×M scores and the τ that
   was configured. Nothing here asks a language model whether it thinks the
   answer is absent.
2. **The hashed body contains no clock.** ``created_at`` sits in the envelope,
   next to the digest it protects, never inside it — and honours
   ``SOURCE_DATE_EPOCH`` so two runs can be compared byte for byte.

The adjudication block (optional chat-model cross-check of the top candidates)
is auxiliary by construction: it lives outside the hash, and removing it changes
neither the verdict nor ``core_sha256``.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import DISTRIBUTION, __version__
from . import ingest
from .ingest import INDEX_NAME, IngestResult, canonical_json, sha256_hex
from .render import render_ledger
from .scan import ScanConfig, ScanOutcome, matrix_payload, run_scan
from .variants import Variant, generate_variants, normalize_claim

SCHEMA = "absentia.attestation/1"

PRESENT = "PRESENT"
ABSENT_BOUNDED = "ABSENT_BOUNDED"
INCONCLUSIVE = "INCONCLUSIVE"

DEFAULT_TAU = 0.62

# Fixed table shipped with the package; v0.1 never calibrates against the
# operator's corpus, because a private archive has no labelled query set. These
# are conservative planning values attached to the shipped thresholds — the
# ledger prints the one in force so a reader knows exactly what is claimed.
CALIBRATION_TABLE: tuple[tuple[float, float], ...] = (
    (0.50, 0.79),
    (0.55, 0.82),
    (0.62, 0.86),
    (0.70, 0.90),
    (0.80, 0.94),
)

ADJUDICATION_SAMPLES = 3
ADJUDICATION_EXCERPT = 160


def calibration_recall(tau: float) -> float:
    """Recall estimate for the nearest published threshold at or below ``tau``."""
    selected = CALIBRATION_TABLE[0][1]
    for threshold, recall in CALIBRATION_TABLE:
        if tau >= threshold:
            selected = recall
    return selected


@dataclass(frozen=True)
class CheckRequest:
    claim: str
    archive: Path
    out_dir: Path
    tau: float = DEFAULT_TAU
    endpoint: str | None = None
    embed_model: str = "bge-m3"
    model: str | None = None
    adjudicate: bool = False

    @classmethod
    def from_env(
        cls,
        claim: str,
        archive: Path,
        out_dir: Path,
        *,
        tau: float | None = None,
        endpoint: str | None = None,
        embed_model: str | None = None,
        model: str | None = None,
        adjudicate: bool = False,
    ) -> "CheckRequest":
        def pick(value, variable, default):
            if value is not None:
                return value
            return os.environ.get(variable) or default

        resolved_tau = float(pick(tau, "ABSENTIA_TAU", DEFAULT_TAU))
        return cls(
            claim=claim,
            archive=archive,
            out_dir=out_dir,
            tau=resolved_tau,
            endpoint=pick(endpoint, "ABSENTIA_ENDPOINT", None) or None,
            embed_model=pick(embed_model, "ABSENTIA_EMBED_MODEL", "bge-m3"),
            model=pick(model, "ABSENTIA_MODEL", None) or None,
            adjudicate=adjudicate,
        )


def _created_at() -> str:
    epoch = (os.environ.get("SOURCE_DATE_EPOCH") or "").strip()
    if epoch.isdigit():
        moment = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    else:
        moment = datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# verdict
# --------------------------------------------------------------------------- #


def decide(top1_score: float, tau: float, chunks: int, parsed_pages: int, lexical_only: bool) -> dict:
    if chunks == 0 or parsed_pages == 0:
        return {"label": INCONCLUSIVE, "reason": "empty_corpus", "top1_score": top1_score}
    if top1_score >= tau:
        return {"label": PRESENT, "reason": "score_at_or_above_tau", "top1_score": top1_score}
    if lexical_only:
        # A literal miss is not an absence: without the semantic channel we
        # cannot claim the other phrasings were covered.
        return {"label": INCONCLUSIVE, "reason": "lexical_only", "top1_score": top1_score}
    return {"label": ABSENT_BOUNDED, "reason": "all_scores_below_tau", "top1_score": top1_score}


# --------------------------------------------------------------------------- #
# chunk citations
# --------------------------------------------------------------------------- #


def _chunk_row(db_path: Path, key: str | None) -> dict | None:
    if key is None:
        return None
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT key, path, page, text FROM chunks WHERE key = ?", (key,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return {"chunk": row[0], "path": row[1], "page": row[2], "text": row[3]}


# --------------------------------------------------------------------------- #
# optional adjudication (auxiliary, outside the hash)
# --------------------------------------------------------------------------- #


def _adjudicate(request: CheckRequest, claim: str, samples: list[dict]) -> dict:
    block = {
        "performed": False,
        "model": request.model,
        "samples": samples,
        "agreement": None,
        "error": None,
    }
    if not request.adjudicate or not request.endpoint or not request.model or not samples:
        return block

    import httpx

    url = request.endpoint.rstrip("/") + "/chat/completions"
    votes: list[bool] = []
    with httpx.Client(timeout=60.0) as client:
        for sample in samples:
            prompt = (
                "判断下面的档案片段是否直接支持该命题。只回答 relevant 或 irrelevant。\n"
                f"命题：{claim}\n片段：{sample['excerpt']}"
            )
            try:
                response = client.post(
                    url,
                    json={
                        "model": request.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0,
                    },
                )
                response.raise_for_status()
                text = response.json()["choices"][0]["message"]["content"].lower()
            except Exception as exc:  # auxiliary only; never fail the run
                block["error"] = type(exc).__name__
                return block
            votes.append("irrelevant" not in text and "relevant" in text)
    block["performed"] = True
    block["agreement"] = round(sum(votes) / len(votes), 4)
    return block


def _samples(
    db_path: Path, outcome: ScanOutcome, limit: int = ADJUDICATION_SAMPLES
) -> list[dict]:
    keys: list[str] = []
    for entry in outcome.per_variant:
        key = entry["top1_chunk"]
        if key and key not in keys:
            keys.append(key)
    samples: list[dict] = []
    for key in keys[:limit]:
        row = _chunk_row(db_path, key)
        if row is None:
            continue
        samples.append(
            {
                "chunk": row["chunk"],
                "path": row["path"],
                "page": row["page"],
                "score": next(
                    entry["top1_score"]
                    for entry in outcome.per_variant
                    if entry["top1_chunk"] == key
                ),
                "excerpt": row["text"][:ADJUDICATION_EXCERPT],
            }
        )
    return samples


# --------------------------------------------------------------------------- #
# attestation assembly
# --------------------------------------------------------------------------- #


def build_core(
    request: CheckRequest,
    ing: IngestResult,
    outcome: ScanOutcome,
    verdict: dict,
) -> dict:
    return {
        "claim": {
            "text": request.claim,
            "normalized": normalize_claim(request.claim),
        },
        "variants": [
            {"text": variant.text, "source": variant.source, "rationale": variant.rationale}
            for variant in outcome.variants
        ],
        "corpus": {
            "archive": str(request.archive),
            "docs": ing.totals["docs"],
            "pages": ing.totals["pages"],
            "parsed_pages": ing.totals["parsed_pages"],
            "chunks": ing.totals["chunks"],
            "excluded": [
                {"path": entry.path, "reason": entry.reason, "pages": entry.pages}
                for entry in ing.excluded
            ],
            "manifest_sha256": ing.manifest["manifest_sha256"],
        },
        "matrix": matrix_payload(outcome),
        "per_variant": [dict(entry) for entry in outcome.per_variant],
        "bound": {
            "embedding_model": outcome.embedding_model,
            "tau": request.tau,
            "calibration_recall": calibration_recall(request.tau),
            "lexical_only": outcome.lexical_only,
            **({"embedding_error": outcome.embedding_error} if outcome.embedding_error else {}),
        },
        "verdict": {
            "label": verdict["label"],
            "reason": verdict["reason"],
            "top1_score": outcome.top1_score,
            "top1_chunk": outcome.top1_chunk,
            "top1_variant": outcome.top1_variant,
            "tau": request.tau,
        },
    }


def run_check(request: CheckRequest) -> dict:
    """Ingest, score, decide, write ``attestation.json`` + ``ledger.md``."""
    if not request.archive.is_dir():
        raise FileNotFoundError(f"archive not found: {request.archive}")

    ing = ingest.run_index(request.archive, request.out_dir)
    db_path = request.out_dir / INDEX_NAME

    variants: list[Variant] = generate_variants(request.claim)
    outcome = run_scan(
        db_path,
        variants,
        ScanConfig(
            tau=request.tau,
            embedding_model=request.embed_model,
            endpoint=request.endpoint,
        ),
        chunk_texts=[(chunk.key, chunk.text) for chunk in ing.chunks],
    )
    verdict = decide(
        outcome.top1_score,
        request.tau,
        outcome.chunks,
        ing.totals["parsed_pages"],
        outcome.lexical_only,
    )
    core = build_core(request, ing, outcome, verdict)
    core_sha = sha256_hex(canonical_json(core).encode("utf-8"))

    samples = _samples(db_path, outcome)
    auxiliary = {
        "adjudication": _adjudicate(request, request.claim, samples),
        "invocation": {
            "endpoint": request.endpoint,
            "embed_model": request.embed_model,
            "model": request.model,
            "tau": request.tau,
        },
        "reproduce": "absentia verify <this file>",
        "citation": _chunk_row(db_path, outcome.top1_chunk),
    }

    envelope = {
        "schema": SCHEMA,
        "created_at": _created_at(),
        "tool": {"name": DISTRIBUTION, "version": __version__},
        "core_sha256": core_sha,
        "core": core,
        "auxiliary": auxiliary,
    }

    request.out_dir.mkdir(parents=True, exist_ok=True)
    (request.out_dir / "attestation.json").write_text(
        canonical_json(envelope) + "\n", encoding="utf-8"
    )
    (request.out_dir / "ledger.md").write_text(
        render_ledger(envelope), encoding="utf-8"
    )
    return envelope


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #


def _compare(claimed_core: dict, recomputed_core: dict) -> list[str]:
    differences: list[str] = []
    pairs = (
        ("manifest_sha256", claimed_core["corpus"]["manifest_sha256"],
         recomputed_core["corpus"]["manifest_sha256"]),
        ("matrix.sha256", claimed_core["matrix"]["sha256"], recomputed_core["matrix"]["sha256"]),
        ("verdict.label", claimed_core["verdict"]["label"], recomputed_core["verdict"]["label"]),
        ("verdict.top1_score", claimed_core["verdict"]["top1_score"],
         recomputed_core["verdict"]["top1_score"]),
    )
    for name, claimed, recomputed in pairs:
        if claimed != recomputed:
            differences.append(f"{name}: claimed={claimed!r} recomputed={recomputed!r}")
    if claimed_core["corpus"]["pages"] != recomputed_core["corpus"]["pages"]:
        differences.append(
            "corpus.pages: claimed={} recomputed={}".format(
                claimed_core["corpus"]["pages"], recomputed_core["corpus"]["pages"]
            )
        )
    return differences


def run_verify(
    attestation_path: Path,
    *,
    archive: Path | None = None,
    tau: float | None = None,
    endpoint: str | None = None,
    embed_model: str | None = None,
) -> dict:
    """Recompute the attestation from the archive and report agreement.

    Nothing is written to the archive: ingestion happens in a temporary
    directory so a verifier cannot alter what they are verifying.
    """
    import json
    import tempfile

    envelope = json.loads(attestation_path.read_text(encoding="utf-8"))
    core = envelope["core"]

    claimed_digest = envelope.get("core_sha256", "")
    actual_digest = sha256_hex(canonical_json(core).encode("utf-8"))
    report: dict = {
        "schema": "absentia.verification/1",
        "attestation": str(attestation_path),
        "claimed_core_sha256": claimed_digest,
        "recomputed_core_sha256": None,
        "agreement": False,
        "differences": [],
        "claimed_verdict": core["verdict"]["label"],
        "recomputed_verdict": None,
    }
    if claimed_digest != actual_digest:
        report["differences"].append("envelope: core_sha256 does not match the body it covers")
        return report

    target_archive = archive or Path(core["corpus"]["archive"])
    if not target_archive.is_dir():
        raise FileNotFoundError(f"archive not found: {target_archive}")

    request_tau = core["bound"]["tau"] if tau is None else tau
    resolved_endpoint = endpoint if endpoint is not None else os.environ.get("ABSENTIA_ENDPOINT") or None
    resolved_embed = embed_model or core["bound"]["embedding_model"] or "bge-m3"

    with tempfile.TemporaryDirectory(prefix="absentia-verify-") as tmp:
        out_dir = Path(tmp)
        ing = ingest.run_index(target_archive, out_dir)
        db_path = out_dir / INDEX_NAME
        variants = generate_variants(core["claim"]["text"])
        outcome = run_scan(
            db_path,
            variants,
            ScanConfig(tau=request_tau, embedding_model=resolved_embed, endpoint=resolved_endpoint),
            chunk_texts=[(chunk.key, chunk.text) for chunk in ing.chunks],
        )
        verdict = decide(
            outcome.top1_score,
            request_tau,
            outcome.chunks,
            ing.totals["parsed_pages"],
            outcome.lexical_only,
        )
        # Keep the claimed archive string so the recomputed core is comparable
        # even when the verifier mounted the archive somewhere else.
        recomputed_core = build_core(
            CheckRequest(
                claim=core["claim"]["text"],
                archive=Path(core["corpus"]["archive"]),
                out_dir=out_dir,
                tau=request_tau,
                endpoint=resolved_endpoint,
                embed_model=resolved_embed,
            ),
            ing,
            outcome,
            verdict,
        )

    report["recomputed_core_sha256"] = sha256_hex(
        canonical_json(recomputed_core).encode("utf-8")
    )
    report["recomputed_verdict"] = verdict["label"]
    report["differences"] = _compare(core, recomputed_core)
    report["agreement"] = not report["differences"]
    if recomputed_core["bound"]["lexical_only"] and core["bound"]["embedding_model"]:
        report["note"] = (
            "verify ran without an embedding endpoint; pass --endpoint (or set "
            "ABSENTIA_ENDPOINT) to reproduce the semantic channel"
        )
    return report
