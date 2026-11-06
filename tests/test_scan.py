"""Tests for m1 (manifest), m2 (scoring + verdict + verify) and the CLI surface.

Semantic assertions use a stand-in embedder: a deterministic hashing embedder
over a hand-written concept map. It stands in for a real local model (bge-m3)
so the suite runs with no Ollama process; the *mechanism* under test — every
chunk scored, lexical and semantic channels combined, verdict recomputable — is
the production one. Where a real model would report a calibrated cosine, the
stand-in reports a coarser number, so nothing here asserts on τ calibration
itself.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from absentia import attest, ingest, mcp_server, scan
from absentia.cli import app

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_ARCHIVE = REPO_ROOT / "fixtures" / "seed_archive"

ABSENT_CLAIM = "Flock 车牌识别系统采购"
PRESENT_CLAIM = "道路监控摄像头采购"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def install_fake_embedder(monkeypatch: pytest.MonkeyPatch, concepts: dict[str, str] | None = None) -> None:
    """Replace the OpenAI-compatible client with a deterministic concept embedder."""
    mapping = concepts or {
        "摄像头": "camera",
        "视频监控设备": "camera",
        "露天焚烧": "burn",
        "秸秆": "burn",
        "火点": "fire",
        "火情": "fire",
        "巡查": "patrol",
        "巡田": "patrol",
        "经费": "spend",
        "资金": "spend",
        "支出": "spend",
    }
    dimension = 256

    def vector(text: str) -> list[float]:
        values = [0.0] * dimension
        lowered = text.lower()
        for surface, concept in mapping.items():
            for _ in range(lowered.count(surface)):
                slot = int.from_bytes(
                    hashlib.blake2b(concept.encode("utf-8"), digest_size=4).digest(), "big"
                ) % dimension
                values[slot] += 1.0
        for token in lowered.split():
            if token.isascii() and len(token) >= 3:
                slot = int.from_bytes(
                    hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest(), "big"
                ) % dimension
                values[slot] += 1.0
        return values

    def fake_request(texts, *, endpoint, model, timeout=60.0, batch_size=32):
        return [vector(text) for text in texts]

    monkeypatch.setattr(scan, "request_embeddings", fake_request)


def request(claim: str, archive: Path, out_dir: Path, **kwargs) -> attest.CheckRequest:
    return attest.CheckRequest.from_env(claim, archive, out_dir, **kwargs)


def trigrams(text: str) -> set[str]:
    return {text[index : index + 3] for index in range(len(text) - 2)}


def minimal_text_pdf(text: str) -> bytes:
    """A hand-built single-page PDF with a real text layer, valid xref."""
    content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


# --------------------------------------------------------------------------- #
# m1 — manifest
# --------------------------------------------------------------------------- #


def test_manifest_is_byte_identical_across_runs(tmp_path: Path) -> None:
    first = ingest.run_index(SEED_ARCHIVE, tmp_path / "a")
    second = ingest.run_index(SEED_ARCHIVE, tmp_path / "b")

    assert (tmp_path / "a" / ingest.MANIFEST_NAME).read_bytes() == (
        tmp_path / "b" / ingest.MANIFEST_NAME
    ).read_bytes()
    assert first.manifest["manifest_sha256"] == second.manifest["manifest_sha256"]
    assert first.totals["chunks"] == second.totals["chunks"]
    assert first.totals["chunks"] > 10
    assert first.totals["parsed_pages"] > 5


def test_manifest_carries_no_absolute_paths(tmp_path: Path) -> None:
    result = ingest.run_index(SEED_ARCHIVE, tmp_path)
    payload = (tmp_path / ingest.MANIFEST_NAME).read_text(encoding="utf-8")
    assert str(SEED_ARCHIVE) not in payload
    assert all("/" != doc.path[0] for doc in result.docs)


def test_index_uses_fts5_trigram(tmp_path: Path) -> None:
    ingest.run_index(SEED_ARCHIVE, tmp_path)
    connection = sqlite3.connect(tmp_path / ingest.INDEX_NAME)
    try:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'chunks_fts'"
        ).fetchone()[0]
        count = connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
    finally:
        connection.close()
    assert "tokenize='trigram'" in sql.replace(" ", "").replace('"', "'")
    assert count > 10


def test_parse_ledger_records_each_exclusion(tmp_path: Path) -> None:
    from pypdf import PdfReader, PdfWriter

    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "notes.md").write_text("# 记录\n会议同意采购办公桌椅。", encoding="utf-8")
    (archive / "empty.md").write_text("", encoding="utf-8")
    (archive / "picture.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    plain_pdf = archive / "plain.pdf"
    plain_pdf.write_bytes(minimal_text_pdf("camera purchase approved"))

    for name, user_password in (("open_aes.pdf", ""), ("locked.pdf", "secret")):
        reader = PdfReader(str(plain_pdf))
        writer = PdfWriter()
        writer.append(reader)
        writer.encrypt(user_password=user_password, owner_password="owner", algorithm="AES-128")
        with (archive / name).open("wb") as handle:
            writer.write(handle)

    result = ingest.build_manifest(archive)
    reasons = {entry.path: entry.reason for entry in result.excluded}

    assert reasons["locked.pdf"] == "encrypted"
    assert reasons["picture.png"] == "unsupported_type"
    assert reasons["empty.md"] == "empty"
    # Empty-user-password AES decrypts and is indexed like any other PDF.
    statuses = {doc.path: (doc.status, doc.pages) for doc in result.docs}
    assert "open_aes.pdf" not in reasons
    assert statuses["open_aes.pdf"] == ("parsed", 1)
    assert statuses["plain.pdf"] == ("parsed", 1)
    parsed = [chunk.text for chunk in result.chunks if chunk.path in {"plain.pdf", "open_aes.pdf"}]
    assert len(parsed) == 2
    assert all("camera" in text for text in parsed)


# --------------------------------------------------------------------------- #
# m2 — scoring and verdicts
# --------------------------------------------------------------------------- #


def test_present_verdict_cites_the_chunk(tmp_path: Path) -> None:
    envelope = attest.run_check(request(PRESENT_CLAIM, SEED_ARCHIVE, tmp_path))
    core = envelope["core"]

    assert core["verdict"]["label"] == attest.PRESENT
    assert core["verdict"]["top1_score"] >= core["bound"]["tau"]
    citation = envelope["auxiliary"]["citation"]
    assert citation["path"] == "procurement_ledger.md"
    assert citation["page"] >= 1
    assert "摄像头" in citation["text"]
    assert core["matrix"]["comparisons"] == core["matrix"]["chunks"] * core["matrix"]["variants"]
    assert core["bound"]["lexical_only"] is True  # no endpoint configured


def test_absent_bounded_needs_the_semantic_channel(tmp_path: Path, monkeypatch) -> None:
    install_fake_embedder(monkeypatch)
    envelope = attest.run_check(
        request(ABSENT_CLAIM, SEED_ARCHIVE, tmp_path, endpoint="http://127.0.0.1:1/v1")
    )
    core = envelope["core"]

    assert core["verdict"]["label"] == attest.ABSENT_BOUNDED
    assert core["verdict"]["reason"] == "all_scores_below_tau"
    assert core["bound"]["lexical_only"] is False
    assert core["bound"]["embedding_model"] == "bge-m3"
    assert core["verdict"]["top1_score"] < core["bound"]["tau"]
    # Coverage is complete: every chunk was compared against every variant.
    assert core["matrix"]["comparisons"] == core["per_variant"][0]["chunks_scored"] * len(
        core["variants"]
    )
    assert core["per_variant"][0]["above_tau"] == 0
    assert core["corpus"]["parsed_pages"] == core["corpus"]["pages"]


def test_lexical_only_never_claims_absence(tmp_path: Path) -> None:
    envelope = attest.run_check(request(ABSENT_CLAIM, SEED_ARCHIVE, tmp_path))
    core = envelope["core"]

    assert core["bound"]["lexical_only"] is True
    assert core["bound"]["embedding_model"] is None
    assert core["verdict"]["label"] == attest.INCONCLUSIVE
    assert core["verdict"]["reason"] == "lexical_only"


def test_two_char_cjk_uses_the_like_fallback(tmp_path: Path) -> None:
    envelope = attest.run_check(request("采购", SEED_ARCHIVE, tmp_path))
    core = envelope["core"]
    assert core["per_variant"][0]["lexical_path"] == "like_fallback"
    assert core["verdict"]["label"] == attest.PRESENT


def test_semantic_match_without_any_three_character_overlap(tmp_path: Path, monkeypatch) -> None:
    """Acceptance (b): the trigger shares no 3-char run with the target chunk."""
    archive = tmp_path / "semantic_archive"
    archive.mkdir()
    body = (
        "# 会议记录\n"
        "会议同意镇农业农村办公室在农忙季节雇用临时人员巡田，"
        "对发现的火情于当天处置，相关支出从秸秆综合利用资金中列支。"
    )
    (archive / "minutes.md").write_text(body, encoding="utf-8")

    claim = "露天焚烧火点巡查经费"
    install_fake_embedder(monkeypatch)

    envelope = attest.run_check(
        request(claim, archive, tmp_path / "out", endpoint="http://127.0.0.1:1/v1")
    )
    core = envelope["core"]
    chunk_text = envelope["auxiliary"]["citation"]["text"]

    assert not (trigrams(claim) & trigrams(chunk_text)), "claim and chunk share a 3-char run"
    base = core["per_variant"][0]
    assert base["lexical_path"] == "none"
    assert base["fired_by"] == "semantic"
    assert base["top1_score"] >= core["bound"]["tau"]
    assert core["verdict"]["label"] == attest.PRESENT


def test_tau_change_moves_verdict_and_bound(tmp_path: Path, monkeypatch) -> None:
    install_fake_embedder(monkeypatch)
    claim = "Flock 摄像头采购"

    low = attest.run_check(
        request(claim, SEED_ARCHIVE, tmp_path / "low", tau=0.1, endpoint="http://127.0.0.1:1/v1")
    )["core"]
    high = attest.run_check(
        request(claim, SEED_ARCHIVE, tmp_path / "high", tau=0.62, endpoint="http://127.0.0.1:1/v1")
    )["core"]

    assert low["bound"]["tau"] == 0.1
    assert high["bound"]["tau"] == 0.62
    assert low["bound"]["calibration_recall"] != high["bound"]["calibration_recall"]
    assert low["verdict"]["label"] == attest.PRESENT
    assert high["verdict"]["label"] == attest.ABSENT_BOUNDED
    assert high["verdict"]["top1_score"] < 0.62


def test_attestation_is_byte_identical_for_the_same_inputs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")
    install_fake_embedder(monkeypatch)
    out_a, out_b = tmp_path / "a", tmp_path / "b"

    first = attest.run_check(
        request(ABSENT_CLAIM, SEED_ARCHIVE, out_a, endpoint="http://127.0.0.1:1/v1")
    )
    second = attest.run_check(
        request(ABSENT_CLAIM, SEED_ARCHIVE, out_b, endpoint="http://127.0.0.1:1/v1")
    )

    assert (out_a / "attestation.json").read_bytes() == (out_b / "attestation.json").read_bytes()
    assert first["created_at"] == "2026-01-01T00:00:00Z"
    assert first["core_sha256"] == second["core_sha256"]
    # The clock lives outside the hash.
    assert first["created_at"] not in json.dumps(first["core"], ensure_ascii=False)


# --------------------------------------------------------------------------- #
# m2 — verify
# --------------------------------------------------------------------------- #


def test_verify_recomputes_and_agrees(tmp_path: Path, monkeypatch) -> None:
    install_fake_embedder(monkeypatch)
    out_dir = tmp_path / "out"
    envelope = attest.run_check(
        request(ABSENT_CLAIM, SEED_ARCHIVE, out_dir, endpoint="http://127.0.0.1:1/v1")
    )

    report = attest.run_verify(
        out_dir / "attestation.json", endpoint="http://127.0.0.1:1/v1"
    )

    assert report["agreement"] is True
    assert report["differences"] == []
    assert report["recomputed_core_sha256"] == envelope["core_sha256"]
    assert report["recomputed_verdict"] == attest.ABSENT_BOUNDED


def test_verify_detects_a_tampered_verdict(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    attest.run_check(request(PRESENT_CLAIM, SEED_ARCHIVE, out_dir))
    path = out_dir / "attestation.json"

    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["core"]["verdict"]["label"] = attest.ABSENT_BOUNDED
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

    report = attest.run_verify(path)
    assert report["agreement"] is False
    assert any("core_sha256" in difference for difference in report["differences"])


def test_verify_reports_a_missing_semantic_channel(tmp_path: Path, monkeypatch) -> None:
    install_fake_embedder(monkeypatch)
    out_dir = tmp_path / "out"
    attest.run_check(
        request(ABSENT_CLAIM, SEED_ARCHIVE, out_dir, endpoint="http://127.0.0.1:1/v1")
    )
    monkeypatch.delenv("ABSENTIA_ENDPOINT", raising=False)

    report = attest.run_verify(out_dir / "attestation.json")
    assert report["agreement"] is False
    assert report["recomputed_verdict"] == attest.INCONCLUSIVE
    assert "endpoint" in report.get("note", "")


# --------------------------------------------------------------------------- #
# CLI + MCP surface
# --------------------------------------------------------------------------- #


def test_cli_index_check_verify(tmp_path: Path) -> None:
    runner = CliRunner()
    out_dir = tmp_path / ".absentia"

    indexed = runner.invoke(app, ["index", str(SEED_ARCHIVE), "--out", str(out_dir)])
    assert indexed.exit_code == 0, indexed.output
    assert "段" in indexed.output
    assert "manifest_sha256" in indexed.output

    checked = runner.invoke(
        app,
        ["check", PRESENT_CLAIM, "--archive", str(SEED_ARCHIVE), "--out", str(out_dir)],
    )
    assert checked.exit_code == 0, checked.output
    assert "PRESENT" in checked.output
    assert (out_dir / "attestation.json").is_file()
    assert (out_dir / "ledger.md").is_file()

    verified = runner.invoke(app, ["verify", str(out_dir / "attestation.json")])
    assert verified.exit_code == 0, verified.output
    assert "AGREES" in verified.output


def test_cli_json_output_is_the_attestation(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "check",
            PRESENT_CLAIM,
            "--archive",
            str(SEED_ARCHIVE),
            "--out",
            str(tmp_path / "out"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["schema"] == attest.SCHEMA
    assert envelope["core"]["verdict"]["label"] == attest.PRESENT


def test_mcp_server_lists_tools_and_runs_a_check(tmp_path: Path) -> None:
    frames = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "absence_check",
                "arguments": {
                    "claim": PRESENT_CLAIM,
                    "archive": str(SEED_ARCHIVE),
                    "out_dir": str(tmp_path / "mcp-out"),
                },
            },
        },
    ]
    stdin = io.StringIO("\n".join(json.dumps(frame, ensure_ascii=False) for frame in frames) + "\n")
    stdout = io.StringIO()

    mcp_server.serve(stdin=stdin, stdout=stdout)

    replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert [reply["id"] for reply in replies] == [1, 2, 3]
    assert replies[0]["result"]["serverInfo"]["name"] == "absentia"

    names = {tool["name"] for tool in replies[1]["result"]["tools"]}
    assert names == {"absence_check", "absence_verify"}

    payload = replies[2]["result"]["structuredContent"]
    assert payload["verdict"] == attest.PRESENT
    assert payload["core"]["matrix"]["comparisons"] > 0
    assert Path(payload["attestation_path"]).is_file()


def test_mcp_verify_tool_reports_agreement(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    attest.run_check(request(PRESENT_CLAIM, SEED_ARCHIVE, out_dir))

    response = mcp_server.handle(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "absence_verify",
                "arguments": {"attestation": str(out_dir / "attestation.json")},
            },
        }
    )
    payload = response["result"]["structuredContent"]
    assert payload["agreement"] is True
    assert payload["summary"].startswith("AGREES")
