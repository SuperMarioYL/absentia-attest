# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-06

First public release. CLI command is `absentia`; the distribution is
`absentia-attest`.

### Added — m1_index_manifest

- `absentia index <archive>` parses text-layer PDF, DOCX, TXT and Markdown,
  paginates, chunks with overlap, and writes `manifest.json` plus
  `index.sqlite` under `.absentia/`.
- The manifest carries a `manifest_sha256` over every document hash, page
  count and chunk hash. It contains no absolute paths and no timestamps, so a
  rerun on the same tree is byte-identical.
- Unreviewable files are never dropped silently: password-protected PDFs,
  documents without a text layer, unsupported types, empty files and parse
  errors each land in the `excluded` ledger with a reason and, when known, a
  page count. AES-encrypted PDFs with an empty user password are decrypted
  transparently and indexed.

### Added — m2_scan_attest

- `absentia check <claim> --archive <dir>` builds a deterministic variant set
  (verbatim, simplified/traditional, full name/alias, officialese and
  synonymous verbs) and scores **every** chunk against **every** variant.
- Hybrid scoring: FTS5 with `tokenize='trigram'` plus an IDF-weighted term
  coverage score, a documented `LIKE` fallback for two-character CJK terms,
  and cosine similarity against a local OpenAI-compatible embedding endpoint.
  The two channels combine with `max`; the ledger records which one fired.
- Verdicts are `PRESENT`, `ABSENT_BOUNDED` or `INCONCLUSIVE`, computed from
  numbers only. The language model is not in the decision path. A lexical-only
  run refuses to claim absence and reports itself as `INCONCLUSIVE`.
- `absentia verify <attestation.json>` re-ingests the archive in a temporary
  directory and compares the whole `core` body and its digest. It only
  contacts a recorded embedding endpoint automatically when it is a loopback
  address. Exit codes: 0 agrees, 3 disagrees, 4 inconclusive.
- `ledger.md` renders a one-page markdown ledger suitable for pasting into a
  workpaper or screenshotting.
- `--tau` changes the verdict and the recorded bound together. `--json` prints
  the complete attestation.

### Added — m3_serve_mcp

- `absentia mcp --archive <dir>` starts a stdio MCP server implementing
  newline-delimited JSON-RPC 2.0, with no SDK dependency.
- Tools `absence_check` and `absence_verify` return the attestation or the
  verification report as structured content.

### Fixed

- `verify` rejects a body that was edited and then re-hashed, and replays the
  invocation recorded in the attestation instead of silently dropping the
  semantic channel.
- `verify` refuses to send archive text to a non-loopback endpoint recorded in
  an untrusted attestation without an explicit `--endpoint`.

[0.1.0]: https://github.com/SuperMarioYL/absentia-attest/releases/tag/v0.1.0
