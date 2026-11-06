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


class AttestationError(Exception):
    """The file is not a readable, structurally valid attestation."""


def _field(mapping: dict, *path: str):
    value = mapping
    travelled: list[str] = []
    for key in path:
        travelled.append(key)
        if not isinstance(value, dict) or key not in value:
            raise AttestationError(
                "不是可复算的 absentia 凭证：缺少字段 " + ".".join(travelled)
            )
        value = value[key]
    return value


def _load_envelope(path: Path) -> tuple[dict, dict, str]:
    import json

    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise AttestationError(f"无法读取凭证 {path}：{exc}") from exc
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AttestationError(
            f"不是合法的 JSON（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
        ) from exc
    if not isinstance(envelope, dict):
        raise AttestationError("不是 absentia 凭证：顶层不是 JSON 对象")
    core = envelope.get("core")
    if not isinstance(core, dict):
        raise AttestationError("不是 absentia 凭证：缺少 core 对象")
    claimed = envelope.get("core_sha256")
    if not isinstance(claimed, str) or not claimed:
        raise AttestationError("不是 absentia 凭证：缺少 core_sha256")
    return envelope, core, claimed


def _core_digest(core: dict) -> str:
    try:
        body = canonical_json(core)
    except (TypeError, ValueError) as exc:
        raise AttestationError(f"凭证 core 无法规范化：{exc}") from exc
    return sha256_hex(body.encode("utf-8"))


def _diff_values(claimed, recomputed, prefix: str = "") -> list[str]:
    """Every leaf that differs, so a forged field can never hide in the noise."""
    if isinstance(claimed, dict) and isinstance(recomputed, dict):
        differences: list[str] = []
        for key in sorted(set(claimed) | set(recomputed)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in claimed:
                differences.append(f"{path}: missing in claimed")
            elif key not in recomputed:
                differences.append(f"{path}: missing in recomputed")
            else:
                differences.extend(_diff_values(claimed[key], recomputed[key], path))
        return differences
    if isinstance(claimed, list) and isinstance(recomputed, list):
        differences = []
        if len(claimed) != len(recomputed):
            differences.append(
                f"{prefix}: length claimed={len(claimed)} recomputed={len(recomputed)}"
            )
        for index, (left, right) in enumerate(zip(claimed, recomputed)):
            differences.extend(_diff_values(left, right, f"{prefix}[{index}]"))
        return differences
    if claimed != recomputed:
        return [f"{prefix}: claimed={claimed!r} recomputed={recomputed!r}"]
    return []


def _is_loopback_endpoint(endpoint: str) -> bool:
    """True for the local-endpoint shapes the product actually documents.

    ``verify`` reads the endpoint out of a file that may be untrusted, and it
    would hand that host the archive's text. Only loopback is contacted
    automatically; a human can opt in to anything else with ``--endpoint``.
    """
    from urllib.parse import urlparse

    host = (urlparse(endpoint).hostname or "").strip("[]").lower()
    return host == "localhost" or host == "::1" or host.startswith("127.")


def run_verify(
    attestation_path: Path,
    *,
    archive: Path | None = None,
    tau: float | None = None,
    endpoint: str | None = None,
    embed_model: str | None = None,
) -> dict:
    """Recompute the attestation from the archive and report agreement.

    The whole ``core`` body is compared, not a hand-picked subset, and the
    recomputed digest is compared with the claimed one. A body edited and then
    re-hashed therefore still disagrees.

    Nothing is written to the archive: ingestion happens in a temporary
    directory so a verifier cannot alter what they are verifying.
    """
    import tempfile

    envelope, core, claimed_digest = _load_envelope(attestation_path)
    body_digest = _core_digest(core)

    verdict_block = core.get("verdict")
    report: dict = {
        "schema": "absentia.verification/1",
        "attestation": str(attestation_path),
        "claimed_core_sha256": claimed_digest,
        "recomputed_core_sha256": None,
        "agreement": False,
        "status": "DISAGREES",
        "differences": [],
        "claimed_verdict": (
            verdict_block.get("label") if isinstance(verdict_block, dict) else None
        ),
        "recomputed_verdict": None,
    }
    if claimed_digest != body_digest:
        report["differences"].append(
            "envelope: core_sha256 does not match the body it covers"
        )
        return report

    claim_text = _field(core, "claim", "text")
    archive_label = _field(core, "corpus", "archive")
    claimed_tau = _field(core, "bound", "tau")
    if not isinstance(claim_text, str) or not claim_text.strip():
        raise AttestationError("不是可复算的 absentia 凭证：claim.text 必须是非空字符串")
    if not isinstance(archive_label, str) or not archive_label:
        raise AttestationError("不是可复算的 absentia 凭证：corpus.archive 必须是非空字符串")
    if isinstance(claimed_tau, bool) or not isinstance(claimed_tau, (int, float)):
        raise AttestationError("不是可复算的 absentia 凭证：bound.tau 必须是数字")

    bound = core.get("bound") if isinstance(core.get("bound"), dict) else {}
    claimed_embed = bound.get("embedding_model")
    claimed_lexical_only = bool(bound.get("lexical_only"))

    auxiliary = envelope.get("auxiliary")
    invocation = auxiliary.get("invocation") if isinstance(auxiliary, dict) else None
    invocation = invocation if isinstance(invocation, dict) else {}

    # The attestation records how it was produced; reproduce that invocation
    # instead of silently dropping the semantic channel. An explicit flag still
    # wins, then a loopback recorded value, then the environment.
    blocked_recorded_endpoint: str | None = None
    if endpoint is not None:
        resolved_endpoint = endpoint
    else:
        recorded_endpoint = invocation.get("endpoint")
        recorded_endpoint = (
            recorded_endpoint
            if isinstance(recorded_endpoint, str) and recorded_endpoint
            else None
        )
        if recorded_endpoint and not _is_loopback_endpoint(recorded_endpoint):
            blocked_recorded_endpoint = recorded_endpoint
            recorded_endpoint = None
        resolved_endpoint = recorded_endpoint or os.environ.get("ABSENTIA_ENDPOINT") or None

    if embed_model is not None:
        resolved_embed = embed_model
    elif isinstance(claimed_embed, str) and claimed_embed:
        resolved_embed = claimed_embed
    else:
        recorded_embed = invocation.get("embed_model")
        resolved_embed = (
            recorded_embed
            if isinstance(recorded_embed, str) and recorded_embed
            else "bge-m3"
        )

    request_tau = claimed_tau if tau is None else tau

    target_archive = archive or Path(archive_label)
    if not target_archive.is_dir():
        raise FileNotFoundError(f"archive not found: {target_archive}")

    with tempfile.TemporaryDirectory(prefix="absentia-verify-") as tmp:
        out_dir = Path(tmp)
        ing = ingest.run_index(target_archive, out_dir)
        db_path = out_dir / INDEX_NAME
        variants = generate_variants(claim_text)
        outcome = run_scan(
            db_path,
            variants,
            ScanConfig(
                tau=request_tau,
                embedding_model=resolved_embed,
                endpoint=resolved_endpoint,
            ),
            chunk_texts=[(chunk.key, chunk.text) for chunk in ing.chunks],
        )
        verdict = decide(
            outcome.top1_score,
            request_tau,
            outcome.chunks,
            ing.totals["parsed_pages"],
            outcome.lexical_only,
        )
        recomputed_core = build_core(
            CheckRequest(
                claim=claim_text,
                archive=Path(archive_label),
                out_dir=out_dir,
                tau=request_tau,
                endpoint=resolved_endpoint,
                embed_model=resolved_embed,
            ),
            ing,
            outcome,
            verdict,
        )

    # The archive path is a label, not content: keep the claimed spelling so a
    # verifier can mount the corpus elsewhere without a spurious difference.
    recomputed_core["corpus"]["archive"] = archive_label

    recomputed_digest = _core_digest(recomputed_core)
    report["recomputed_core_sha256"] = recomputed_digest
    report["recomputed_verdict"] = verdict["label"]
    report["endpoint"] = resolved_endpoint

    if not claimed_lexical_only and recomputed_core["bound"]["lexical_only"]:
        # The claim used the semantic channel and we could not reproduce it.
        # That is a verification precondition, not evidence of tampering.
        error = outcome.embedding_error or "no_endpoint"
        report["status"] = "INCONCLUSIVE"
        report["embedding_error"] = error
        if blocked_recorded_endpoint:
            report["blocked_recorded_endpoint"] = blocked_recorded_endpoint
        pieces = [f"复算未能使用语义通道（{error}）"]
        if blocked_recorded_endpoint:
            pieces.append(
                f"凭证记录的嵌入端点 {blocked_recorded_endpoint} 不是本机地址，"
                "verify 不会主动把档案内容发往该主机；如确认可信，请显式传 --endpoint"
            )
        elif resolved_endpoint:
            pieces.append(f"嵌入端点 {resolved_endpoint} 当前不可用")
        else:
            pieces.append("凭证没有记录可用的嵌入端点")
        pieces.append(f"凭证声明的 embedding_model 为 {claimed_embed!r}")
        pieces.append("这是复核条件不足，不是凭证不一致")
        report["note"] = "；".join(pieces) + "。"
        return report

    differences = _diff_values(core, recomputed_core)
    if recomputed_digest != claimed_digest:
        differences.insert(
            0,
            f"core_sha256: claimed={claimed_digest} recomputed={recomputed_digest}",
        )
    report["differences"] = differences
    report["agreement"] = not differences
    report["status"] = "AGREES" if not differences else "DISAGREES"
    return report
