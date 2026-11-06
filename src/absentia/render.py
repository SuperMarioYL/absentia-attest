"""The one-page ledger — the artefact that gets screenshotted and attached.

Plain markdown on purpose: it survives email, gets pasted into an audit
workpaper, and diffs cleanly in git. Every number in it is read from the
attestation, never recomputed here, so what a reviewer sees is exactly what
``verify`` checks.
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence

from .ingest import split_sentences
from .scan import extract_terms

VERDICT_LABELS = {
    "PRESENT": "命中（有）",
    "ABSENT_BOUNDED": "缺席（有界）",
    "INCONCLUSIVE": "无法判定",
}

REASON_LABELS = {
    "score_at_or_above_tau": "最高分达到阈值",
    "all_scores_below_tau": "全部 N×M 比对低于阈值",
    "lexical_only": "只跑了词法通道（无可用嵌入端点）",
    "empty_corpus": "没有可打分的分段",
}

EXCLUDED_REASONS = {
    "encrypted": "需要口令才能打开",
    "no_text_layer": "无文字层（扫描件）",
    "unsupported_type": "类型不在 v0.1 解析范围",
    "parse_error": "解析失败",
    "empty": "空文件",
}


def _score(value: float) -> str:
    return f"{value:.2f}"


CITATION_MAX_CHARS = 220
# Chunk text is joined without newlines, so a markdown table arrives as
# "… | 43,200 || 2021-06-18 | …". Split on that row boundary too, otherwise the
# whole table is one "sentence" and the excerpt is just a header slice.
_CANDIDATE_BREAK = re.compile(r"\n+|(?<=\|)(?=\|)")


def _citation_excerpt(text: str, terms: Sequence[str]) -> str:
    """The sentence or table row carrying the most query terms.

    The top-ranked chunk often opens with a heading or the tail of an earlier
    paragraph. Cutting the first 220 characters would show that instead of the
    evidence, so pick the unit with the strongest term overlap and only then
    collapse whitespace. The selected words are kept verbatim.
    """
    units: list[str] = []
    for block in _CANDIDATE_BREAK.split(text):
        for unit in split_sentences(block):
            unit = unit.strip()
            if unit:
                units.append(unit)
    if not units:
        return ""

    vocabulary = tuple(term for term in terms if term)

    def rank(item: tuple[int, str]) -> tuple[int, int, int]:
        index, unit = item
        matched = [term for term in vocabulary if term in unit]
        return (len(matched), sum(len(term) for term in matched), -index)

    _, best = max(enumerate(units), key=rank)
    excerpt = re.sub(r"\s+", " ", best).strip()
    if len(excerpt) > CITATION_MAX_CHARS:
        excerpt = excerpt[:CITATION_MAX_CHARS].rstrip() + "…"
    return excerpt


def headline(core: Mapping) -> str:
    corpus = core["corpus"]
    matrix = core["matrix"]
    verdict = core["verdict"]
    bound = core["bound"]
    label = VERDICT_LABELS.get(verdict["label"], verdict["label"])
    unresolved = corpus["pages"] - corpus["parsed_pages"]
    unknown_pages = sum(
        1 for entry in corpus["excluded"] if entry["reason"] == "encrypted" and entry["pages"] == 0
    )
    comparison = "≥" if verdict["top1_score"] >= verdict["tau"] else "<"
    tail = f"未解析 {unresolved:,} 页已列出"
    if unknown_pages:
        tail += f"，另有 {unknown_pages} 个加密文件页数未知"
    return (
        f"{corpus['pages']:,} 页中 {corpus['parsed_pages']:,} 页已解析并逐段打分，"
        f"{matrix['variants']} 种口径改写 × {matrix['chunks']:,} 段 = "
        f"{matrix['comparisons']:,} 次比对，最高相似度 {_score(verdict['top1_score'])} "
        f"{comparison} 阈值 {_score(bound['tau'])}，判定：{label}；"
        f"{tail}"
    )


def summary_line(core: Mapping) -> str:
    """One line for the terminal / MCP response."""
    verdict = core["verdict"]
    bound = core["bound"]
    label = VERDICT_LABELS.get(verdict["label"], verdict["label"])
    if bound["lexical_only"]:
        error = bound.get("embedding_error")
        channel = f"仅词法（端点失败:{error}）" if error else "仅词法"
    else:
        channel = f"词法+语义({bound['embedding_model']})"
    return (
        f"{verdict['label']}（{label}） 最高分 {_score(verdict['top1_score'])} / τ "
        f"{_score(verdict['tau'])} · {core['matrix']['comparisons']:,} 次比对 · {channel}"
    )


def render_ledger(envelope: Mapping) -> str:
    core = envelope["core"]
    corpus = core["corpus"]
    matrix = core["matrix"]
    bound = core["bound"]
    verdict = core["verdict"]
    claim = core["claim"]
    label = VERDICT_LABELS.get(verdict["label"], verdict["label"])
    reason = REASON_LABELS.get(verdict["reason"], verdict["reason"])

    lines: list[str] = []
    lines.append("# 缺席凭证台账")
    lines.append("")
    lines.append(f"> {headline(core)}")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 命题 | {claim['text']} |")
    lines.append(f"| 归一化命题 | {claim['normalized'] or '—'} |")
    lines.append(f"| 判定 | **{label}**（`{verdict['label']}`） |")
    lines.append(f"| 判定依据 | {reason}（`{verdict['reason']}`） |")
    lines.append(f"| 最高相似度 | {_score(verdict['top1_score'])} |")
    lines.append(f"| 阈值 τ | {_score(bound['tau'])} |")
    lines.append(
        f"| 通道 | {'仅词法（lexical_only）' if bound['lexical_only'] else '词法 + 语义'} |"
    )
    lines.append(f"| 嵌入模型 | {bound['embedding_model'] or '未配置'} |")
    if bound.get("embedding_error"):
        lines.append(
            f"| 嵌入端点 | 调用失败（`{bound['embedding_error']}`），语义通道未运行 |"
        )
    lines.append(f"| 固定校准召回 | {bound['calibration_recall']:.2f} |")
    lines.append(f"| 文档 / 页 | {corpus['docs']} / {corpus['pages']} |")
    lines.append(f"| 已解析页 | {corpus['parsed_pages']} |")
    lines.append(f"| 分段 | {matrix['chunks']} |")
    lines.append(f"| 比对次数 | {matrix['comparisons']} |")
    lines.append(f"| manifest_sha256 | `{corpus['manifest_sha256']}` |")
    lines.append(f"| matrix.sha256 | `{matrix['sha256']}` |")
    lines.append(f"| core_sha256 | `{envelope['core_sha256']}` |")
    lines.append(f"| 生成时间（不在哈希内） | {envelope['created_at']} |")
    lines.append("")

    lines.append("## 口径改写")
    lines.append("")
    lines.append("| # | 改写 | 来源 | 理由 |")
    lines.append("| --- | --- | --- | --- |")
    for index, variant in enumerate(core["variants"], start=1):
        lines.append(
            f"| {index} | {variant['text']} | `{variant['source']}` | {variant['rationale']} |"
        )
    lines.append("")

    lines.append("## 逐口径结果")
    lines.append("")
    lines.append("| 改写 | 打分段数 | ≥τ | 最高分 | 最高段 | 词法路径 | 触发通道 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for entry in core["per_variant"]:
        lines.append(
            f"| {entry['variant']} | {entry['chunks_scored']} | {entry['above_tau']} | "
            f"{_score(entry['top1_score'])} | `{entry['top1_chunk'] or '—'}` | "
            f"{entry['lexical_path']} | {entry['fired_by']} |"
        )
    lines.append("")

    citation = (envelope.get("auxiliary") or {}).get("citation")
    if citation:
        terms = extract_terms(verdict.get("top1_variant") or claim["text"])
        excerpt = _citation_excerpt(citation["text"], terms)
        lines.append("## 最高分段落（供人工复核）")
        lines.append("")
        lines.append(f"- 文件：`{citation['path']}` 第 {citation['page']} 页（`{citation['chunk']}`）")
        lines.append(f"- 摘录：{excerpt}")
        lines.append("")

    lines.append("## 未解析台账")
    lines.append("")
    if corpus["excluded"]:
        lines.append("| 文件 | 原因 | 页数 |")
        lines.append("| --- | --- | --- |")
        for entry in corpus["excluded"]:
            human = EXCLUDED_REASONS.get(entry["reason"], entry["reason"])
            lines.append(f"| {entry['path']} | `{entry['reason']}`（{human}） | {entry['pages']} |")
        lines.append("")
        lines.append(
            f"未解析页数 {corpus['pages'] - corpus['parsed_pages']}，"
            "未计入 parsed_pages，也不参与打分。"
        )
    else:
        lines.append("无。全部文件均已解析并进入矩阵。")
    lines.append("")

    adjudication = (envelope.get("auxiliary") or {}).get("adjudication") or {}
    lines.append("## 裁决抽样（辅助，不参与判定）")
    lines.append("")
    if adjudication.get("performed"):
        lines.append(
            f"- 模型：`{adjudication.get('model')}`，一致率 {adjudication.get('agreement')}"
        )
    else:
        lines.append("- 未执行（v0.1 默认关闭；判定不读取模型输出）")
    for sample in adjudication.get("samples", []):
        lines.append(
            f"- `{sample['chunk']}` {sample['path']} 第 {sample['page']} 页 "
            f"（{_score(sample['score'])}）"
        )
    lines.append("")

    lines.append("## 复算")
    lines.append("")
    lines.append("```")
    lines.append("absentia verify .absentia/attestation.json")
    lines.append("```")
    lines.append("")
    lines.append(
        "复算比对 manifest_sha256、matrix.sha256 与判定；"
        "同一档案、同一命题、同一配置应逐字节一致。"
    )
    lines.append("")
    return "\n".join(lines)
