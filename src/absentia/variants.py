"""口径改写 — the M side of the N×M matrix.

v0.1 covers exactly three rewrite families (plan §6): 简繁, 全称/简称/别称 and
公文套话·同义动词. Every variant carries its ``source`` and a ``rationale`` so a
reviewer can see *why* the question was asked that way, and so the same set is
regenerated identically on the verifying machine.

The generator is deterministic and dictionary-only. Model-written variants are
deliberately absent from v0.1: a variant set produced by a sampling model would
not be reproducible, and reproducibility is the whole product.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SIMPLIFIED_TO_TRADITIONAL = {
    "镇": "鎮", "会": "會", "议": "議", "员": "員", "采": "採", "购": "購",
    "摄": "攝", "头": "頭", "审": "審", "经": "經", "费": "費", "预": "預",
    "农": "農", "业": "業", "办": "辦", "执": "執", "队": "隊", "监": "監",
    "设": "設", "备": "備", "无": "無", "机": "機", "资": "資", "项": "項",
    "纪": "紀", "记": "記", "录": "錄", "发": "發", "财": "財", "产": "產",
    "复": "復", "过": "過", "决": "決", "场": "場", "务": "務", "联": "聯",
    "电": "電", "视": "視", "车": "車", "单": "單", "数": "數", "据": "據",
    "动": "動", "规": "規", "划": "劃", "总": "總", "级": "級",
    "识": "識", "别": "別", "统": "統", "处": "處", "证": "證", "号": "號",
    "时": "時", "间": "間", "长": "長", "门": "門", "开": "開", "报": "報",
    "计": "計", "标": "標", "车": "車", "产": "產", "业": "業", "务": "務",
}

TRADITIONAL_TO_SIMPLIFIED = {value: key for key, value in SIMPLIFIED_TO_TRADITIONAL.items()}

# Canonical form first. Used both for rewriting and for segmentation in scan.py.
ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("镇人民政府", "镇政府"),
    ("人民代表大会常务委员会", "人大常委会", "人大"),
    ("综合行政执法队", "城管执法队", "执法队"),
    ("社会事务办公室", "社会事务办"),
    ("农业农村办公室", "农业农村办", "农办"),
    ("无人机", "无人驾驶航空器", "四旋翼飞行器"),
    ("摄像头", "视频监控设备", "监控探头"),
    ("会议纪要", "议事纪要", "会议记录"),
)

VERB_GROUPS: tuple[tuple[str, ...], ...] = (
    ("批准", "审批", "核准", "批复", "同意"),
    ("采购", "购置", "采买", "购入"),
    ("提到", "提及", "载明", "记载"),
    ("配置", "配备", "添置"),
    ("经费", "预算", "资金"),
)

PHRASE_PAIRS: tuple[tuple[str, str], ...] = (
    ("有没有过", "是否曾经"),
    ("有没有", "是否"),
    ("是否", "有无"),
    ("批过", "批准过"),
)

# Extra heads that matter for lexical segmentation but are not rewrite rules.
SEGMENT_EXTRA: tuple[str, ...] = (
    "道路", "监控", "巡查", "排班", "招标", "合同", "审计", "台账", "决议",
    "议题", "预算", "财政", "民政", "综治", "环保", "拆迁", "征地", "补贴",
    "工程", "验收", "拨款", "支出", "收入", "人员", "编制", "年度", "季度",
)


def _all_lexicon_terms() -> list[str]:
    terms: set[str] = set(SEGMENT_EXTRA)
    for group in ALIAS_GROUPS + VERB_GROUPS:
        terms.update(group)
    for left, right in PHRASE_PAIRS:
        terms.update((left, right))
    # Longest first so greedy segmentation never splits a longer match.
    return sorted(terms, key=lambda term: (-len(term), term))


LEXICON_TERMS: tuple[str, ...] = tuple(_all_lexicon_terms())


@dataclass(frozen=True)
class Variant:
    text: str
    source: str
    rationale: str


def to_traditional(text: str) -> str:
    return "".join(SIMPLIFIED_TO_TRADITIONAL.get(char, char) for char in text)


def to_simplified(text: str) -> str:
    return "".join(TRADITIONAL_TO_SIMPLIFIED.get(char, char) for char in text)


_INTERROGATIVES = ("有没有过", "有没有", "是否", "有无", "曾否")
# A year range in a claim is scope, not content: it tells the reader which
# period was asked about, it never appears verbatim in a 纪要.
_YEAR_RANGE = re.compile(r"\d{4}\s*[-–—~～至到]\s*\d{4}\s*年?")
# Filler that survives segmentation but carries no evidence.
EMPTY_TERMS = frozenset({"年间", "期间", "之内", "之中"})


def normalize_claim(text: str) -> str:
    """Collapse whitespace, simplify, and drop semantically empty question words."""
    normalized = to_simplified(text)
    for word in _INTERROGATIVES:
        normalized = normalized.replace(word, "")
    normalized = _YEAR_RANGE.sub(" ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _replace_present(text: str, members: tuple[str, ...], target: str) -> str | None:
    """Replace every present member of a group with ``target``; None if no hit."""
    for member in members:
        if member in text and member != target:
            return text.replace(member, target)
    return None


def generate_variants(claim: str, *, max_variants: int = 10) -> list[Variant]:
    """Deterministic, de-duplicated rewrite set starting from the verbatim claim."""
    variants: list[Variant] = [Variant(claim, "verbatim", "原问题原样进入矩阵")]
    seen = {claim}

    def add(text: str, source: str, rationale: str) -> None:
        if text in seen or len(variants) >= max_variants:
            return
        seen.add(text)
        variants.append(Variant(text, source, rationale))

    traditional = to_traditional(claim)
    add(traditional, "traditional", "简繁归一：以繁体写法重问一遍，覆盖档案简繁错位")

    for group in ALIAS_GROUPS:
        for member in group:
            if member not in claim:
                continue
            for alternative in group:
                if alternative == member:
                    continue
                rewritten = _replace_present(claim, (member,), alternative)
                if rewritten:
                    add(rewritten, "alias", f"全称/简称/别称：{member} → {alternative}")
                break

    for group in VERB_GROUPS:
        for member in group:
            if member not in claim:
                continue
            for alternative in group:
                if alternative == member:
                    continue
                rewritten = _replace_present(claim, (member,), alternative)
                if rewritten:
                    add(rewritten, "verb", f"同义动词：{member} → {alternative}")
                break

    for left, right in PHRASE_PAIRS:
        if left in claim:
            add(claim.replace(left, right), "phrase", f"公文套话：{left} → {right}")

    return variants


def segment(run: str) -> list[str]:
    """Greedy longest-match segmentation against the built-in lexicon.

    Chinese has no spaces, and the lexical channel needs terms it can hand to
    FTS5. Unknown stretches are kept whole rather than chopped into single
    characters, so a novel noun still becomes a searchable term.
    """
    terms: list[str] = []
    index = 0
    while index < len(run):
        match = None
        for term in LEXICON_TERMS:
            if run.startswith(term, index):
                match = term
                break
        if match:
            terms.append(match)
            index += len(match)
            continue
        stop = index
        while stop < len(run) and not any(run.startswith(term, stop) for term in LEXICON_TERMS):
            stop += 1
        if stop == index:
            stop = index + 1
        terms.append(run[index:stop])
        index = stop
    return terms
