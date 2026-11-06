"""m1 — 解析 → 分页 → 切块 → sha256 清单与解析失败台账.

The manifest is the single source of truth for everything downstream. It is a
pure function of the archive's bytes: no timestamps, no absolute paths, no
reliance on filesystem ordering. Rerunning it on the same tree must produce a
byte-identical ``manifest.json``, and ``manifest_sha256`` is the value a third
party re-computes to check that they audited the same corpus.

Parse boundaries are hard-coded per file type (no "try, then silently drop
pages"): text-layer PDF/DOCX/TXT/MD are extracted; AES-encrypted PDFs with an
empty user password are opened transparently by pypdf (it needs the declared
``cryptography`` dependency); everything else — password-protected files,
documents without a text layer, unsupported types, unreadable files — lands in
the ``excluded`` ledger with a reason and, when known, a page count.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .variants import to_simplified

SCHEMA_VERSION = "1"

# A "page" for reflowable formats is a logical page: an explicit form feed when
# the source has one, otherwise a deterministic packing of paragraphs up to this
# size. PDFs use their real pages.
PAGE_TARGET_CHARS = 1500
CHUNK_TARGET_CHARS = 400
CHUNK_OVERLAP_CHARS = 80

TEXT_SUFFIXES = {".txt", ".md", ".markdown"}
SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".absentia", ".venv", "venv"}

MANIFEST_NAME = "manifest.json"
INDEX_NAME = "index.sqlite"


# --------------------------------------------------------------------------- #
# hashing helpers (also used by attest.py — one canonical encoding everywhere)
# --------------------------------------------------------------------------- #


def canonical_json(value: object) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8, no NaN/Inf."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# parse results
# --------------------------------------------------------------------------- #


class ParseExcluded(Exception):
    """Raised when a file cannot be reviewed; carries the ledger reason."""

    def __init__(self, reason: str, pages: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.pages = pages


@dataclass(frozen=True)
class Chunk:
    key: str
    path: str
    page: int
    seq: int
    text: str


@dataclass(frozen=True)
class DocEntry:
    path: str
    sha256: str
    size: int
    pages: int
    status: str
    reason: str | None


@dataclass(frozen=True)
class ExcludedEntry:
    path: str
    reason: str
    pages: int


@dataclass(frozen=True)
class IngestResult:
    docs: tuple[DocEntry, ...]
    chunks: tuple[Chunk, ...]
    excluded: tuple[ExcludedEntry, ...]
    manifest: dict
    manifest_bytes: bytes

    @property
    def totals(self) -> dict:
        return self.manifest["totals"]


# --------------------------------------------------------------------------- #
# text splitting
# --------------------------------------------------------------------------- #

_SENTENCE_ENDERS = "。！？；!?;\n"


def split_sentences(text: str) -> list[str]:
    """Split into sentence-ish units, keeping the terminator on the left unit."""
    units: list[str] = []
    buf: list[str] = []
    for char in text:
        buf.append(char)
        if char in _SENTENCE_ENDERS:
            unit = "".join(buf).strip()
            if unit:
                units.append(unit)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        units.append(tail)
    return units


def _pack(units: list[str], target: int) -> list[str]:
    """Greedily pack units into blocks of at most ``target`` characters."""
    blocks: list[str] = []
    current: list[str] = []
    size = 0
    for unit in units:
        unit = unit.strip()
        if not unit:
            continue
        if current and size + len(unit) + 2 > target:
            blocks.append("\n".join(current))
            current = []
            size = 0
        current.append(unit)
        size += len(unit) + 2
    if current:
        blocks.append("\n".join(current))
    return blocks


def split_pages(text: str) -> list[str]:
    """Deterministic pagination for reflowable text.

    A form feed, when present, is authoritative (the author already told us
    where pages break). Otherwise paragraphs are packed to ``PAGE_TARGET_CHARS``;
    oversized paragraphs fall back to sentence packing.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\f" in text:
        raw = text.split("\f")
    else:
        blocks: list[str] = []
        for paragraph in re.split(r"\n\s*\n", text):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if len(paragraph) > PAGE_TARGET_CHARS:
                blocks.extend(_pack(split_sentences(paragraph), PAGE_TARGET_CHARS))
            else:
                blocks.append(paragraph)
        raw = _pack(blocks, PAGE_TARGET_CHARS)
    return [page.strip() for page in raw if page.strip()]


def split_chunks(text: str) -> list[str]:
    """Break one page into overlapping, sentence-aligned chunks."""
    units: list[str] = []
    for unit in split_sentences(text):
        if len(unit) <= CHUNK_TARGET_CHARS:
            units.append(unit)
        else:  # a single run-on sentence: hard split, no data lost
            step = CHUNK_TARGET_CHARS
            units.extend(unit[i : i + step] for i in range(0, len(unit), step))

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for unit in units:
        if current and size + len(unit) > CHUNK_TARGET_CHARS:
            chunk = "".join(current).strip()
            if chunk:
                chunks.append(chunk)
            tail = chunk[-CHUNK_OVERLAP_CHARS:] if CHUNK_OVERLAP_CHARS else ""
            current = [tail] if tail else []
            size = len(tail)
        current.append(unit)
        size += len(unit)
    final = "".join(current).strip()
    if final:
        chunks.append(final)
    return chunks


# --------------------------------------------------------------------------- #
# per-format extraction
# --------------------------------------------------------------------------- #


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_pdf(path: Path) -> list[str]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # pypdf raises several unrelated types
        raise ParseExcluded("parse_error") from exc

    if reader.is_encrypted:
        # Empty user password is a real, common case (owner-only protection).
        # pypdf needs `cryptography` for AES; without it this raises and the
        # file goes to the ledger rather than being silently skipped.
        try:
            unlocked = int(reader.decrypt("") or 0)
        except Exception as exc:
            raise ParseExcluded("encrypted", _safe_page_count(reader)) from exc
        if not unlocked:
            raise ParseExcluded("encrypted", _safe_page_count(reader))

    try:
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise ParseExcluded("parse_error") from exc

    if not any(page.strip() for page in pages):
        raise ParseExcluded("no_text_layer", len(pages))
    return [page.strip() for page in pages]


def _safe_page_count(reader) -> int:
    try:
        return len(reader.pages)
    except Exception:
        return 0


def _parse_docx(path: Path) -> list[str]:
    import docx

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise ParseExcluded("parse_error") from exc

    parts: list[str] = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    if not parts:
        raise ParseExcluded("no_text_layer", 0)
    return split_pages("\n\n".join(parts))


def _extract_pages(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _parse_pdf(path)
    if suffix == ".docx":
        return _parse_docx(path)
    if suffix in TEXT_SUFFIXES:
        return split_pages(_read_text(path))
    raise ParseExcluded("unsupported_type")


# --------------------------------------------------------------------------- #
# archive walk
# --------------------------------------------------------------------------- #


def discover(archive: Path) -> list[Path]:
    """All reviewable-looking files, in a stable order."""
    found: list[Path] = []
    for path in archive.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in path.relative_to(archive).parts):
            continue
        found.append(path)
    return sorted(found, key=lambda p: p.relative_to(archive).as_posix())


def _chunk_key(file_sha: str, page_no: int, seq: int) -> str:
    return f"{file_sha[:10]}:p{page_no:04d}:c{seq:03d}"


def build_manifest(archive: Path) -> IngestResult:
    """Walk ``archive`` and produce the deterministic manifest plus chunk list."""
    docs: list[DocEntry] = []
    excluded: list[ExcludedEntry] = []
    chunks: list[Chunk] = []

    for path in discover(archive):
        relative = path.relative_to(archive).as_posix()
        size = path.stat().st_size
        file_sha = sha256_hex(path.read_bytes())

        try:
            pages = _extract_pages(path)
        except ParseExcluded as exc:
            docs.append(DocEntry(relative, file_sha, size, 0, "excluded", exc.reason))
            excluded.append(ExcludedEntry(relative, exc.reason, exc.pages))
            continue
        except Exception:
            docs.append(DocEntry(relative, file_sha, size, 0, "excluded", "parse_error"))
            excluded.append(ExcludedEntry(relative, "parse_error", 0))
            continue

        if not pages:
            docs.append(DocEntry(relative, file_sha, size, 0, "excluded", "empty"))
            excluded.append(ExcludedEntry(relative, "empty", 0))
            continue

        docs.append(DocEntry(relative, file_sha, size, len(pages), "parsed", None))
        for page_no, page_text in enumerate(pages, start=1):
            for seq, chunk_text in enumerate(split_chunks(page_text)):
                chunks.append(
                    Chunk(
                        key=_chunk_key(file_sha, page_no, seq),
                        path=relative,
                        page=page_no,
                        seq=seq,
                        text=chunk_text,
                    )
                )

    parsed_docs = [doc for doc in docs if doc.status == "parsed"]
    totals = {
        "docs": len(parsed_docs),
        "excluded_docs": len(excluded),
        "chunks": len(chunks),
        "pages": sum(doc.pages for doc in parsed_docs) + sum(e.pages for e in excluded),
        "parsed_pages": sum(doc.pages for doc in parsed_docs),
    }

    payload = {
        "schema_version": SCHEMA_VERSION,
        "totals": totals,
        "docs": [
            {
                "path": doc.path,
                "sha256": doc.sha256,
                "size": doc.size,
                "pages": doc.pages,
                "status": doc.status,
                "reason": doc.reason,
            }
            for doc in docs
        ],
        "excluded": [
            {"path": e.path, "reason": e.reason, "pages": e.pages} for e in excluded
        ],
        "chunks": [
            {
                "key": chunk.key,
                "path": chunk.path,
                "page": chunk.page,
                "seq": chunk.seq,
                "chars": len(chunk.text),
                "sha256": sha256_hex(chunk.text.encode("utf-8")),
            }
            for chunk in chunks
        ],
    }
    manifest = dict(payload)
    manifest["manifest_sha256"] = sha256_hex(canonical_json(payload).encode("utf-8"))
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )

    return IngestResult(
        docs=tuple(docs),
        chunks=tuple(chunks),
        excluded=tuple(excluded),
        manifest=manifest,
        manifest_bytes=manifest_bytes,
    )


# --------------------------------------------------------------------------- #
# sqlite index (rebuilt from scratch on every run — no incremental state)
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE docs (
    path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    pages INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT
);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    page INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    text TEXT NOT NULL,
    -- Simplified copy used by the lexical channel only: a query written in
    -- either script reaches an archive written in the other. Citations and
    -- embeddings always use the original `text`.
    text_norm TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
-- The tokenizer is a hard constraint, not a default: unicode61 treats a whole
-- run of CJK as one alnum token, so queries like 「摄像头」 match nothing.
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    key UNINDEXED,
    text,
    tokenize='trigram'
);
CREATE TABLE embeddings (
    key TEXT PRIMARY KEY,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL
);
"""


def build_index(result: IngestResult, db_path: Path) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(_SCHEMA)
        connection.executemany(
            "INSERT INTO docs(path, sha256, size, pages, status, reason) VALUES (?,?,?,?,?,?)",
            [
                (doc.path, doc.sha256, doc.size, doc.pages, doc.status, doc.reason)
                for doc in result.docs
            ],
        )
        connection.executemany(
            "INSERT INTO chunks(id, key, path, page, seq, text, text_norm, sha256)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    index + 1,
                    chunk.key,
                    chunk.path,
                    chunk.page,
                    chunk.seq,
                    chunk.text,
                    to_simplified(chunk.text),
                    sha256_hex(chunk.text.encode("utf-8")),
                )
                for index, chunk in enumerate(result.chunks)
            ],
        )
        connection.executemany(
            "INSERT INTO chunks_fts(rowid, key, text) VALUES (?,?,?)",
            [
                (index + 1, chunk.key, to_simplified(chunk.text))
                for index, chunk in enumerate(result.chunks)
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return db_path


def pack_vector(values: list[float]) -> bytes:
    """Normalised float32 vector as a blob — the plan's sqlite-only storage."""
    from array import array

    return array("f", values).tobytes()


def write_manifest(result: IngestResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / MANIFEST_NAME
    target.write_bytes(result.manifest_bytes)
    return target


def run_index(archive: Path, out_dir: Path) -> IngestResult:
    """Full rebuild: manifest + sqlite index. Idempotent by construction."""
    result = build_manifest(archive)
    write_manifest(result, out_dir)
    build_index(result, out_dir / INDEX_NAME)
    return result
