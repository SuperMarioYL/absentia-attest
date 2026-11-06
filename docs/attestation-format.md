# 缺席凭证格式（attestation format v1）

本文写清 `attestation.json` 的每个字段、哪些字节受哈希保护、判定怎么算出来，
以及另一台机器如何复算。读者假定是**要审这份文件的人**，不是本项目的开发者。

---

## 1. 三个文件

`absentia check` 在输出目录（默认 `./.absentia/`）写三样东西：

| 文件 | 作用 | 是否进哈希 |
| --- | --- | --- |
| `manifest.json` | 解析→分页→切块清单与解析失败台账（m1） | 自身有 `manifest_sha256` |
| `attestation.json` | 凭证本体：命题、口径、矩阵摘要、界、判定 | `core` 进 `core_sha256` |
| `ledger.md` | 一页台账，供截图与归档 | 不是事实源，值全部抄自凭证 |
| `index.sqlite` | 本次全量重建的索引（含 FTS5 与向量 blob） | 否，可随时重建 |

`ledger.md` 与 `index.sqlite` 都是派生物。删掉它们不影响复算。

---

## 2. 信封与内核

```jsonc
{
  "schema": "absentia.attestation/1",
  "created_at": "2026-11-06T08:30:00Z",   // 时钟：不在哈希内
  "tool": { "name": "absentia-attest", "version": "0.1.0" },
  "core_sha256": "…",                      // 对 core 的规范化 JSON 取 sha256
  "core": { … },                           // 受保护的本体（见 §3）
  "auxiliary": { … }                       // 辅助信息：裁决抽样、调用参数、引文
}
```

**为什么分信封与内核**：机器名、绝对路径、时间戳、模型抽样结果都会变化。
把它们放进被哈希的本体会让同一份档案在同一天跑出两个摘要，"可复算"就失去意义。
因此只有 `core` 受保护；`auxiliary` 说明"这次是怎么跑的"，不受保护，也不参与判定。

`created_at` 支持 `SOURCE_DATE_EPOCH`（reproducible-builds 约定）：
设置了就从该时间戳取值，于是两次运行连文件字节都一致。

---

## 3. `core` 字段

### 3.1 `claim`

```jsonc
{ "text": "Flock 车牌识别系统采购", "normalized": "Flock 车牌识别系统采购" }
```

`normalized` = 繁转简 + 去掉疑问词（有没有／是否／有无／曾否）+ 去掉年份区间（`2009-2025` 是范围不是内容）+ 压缩空白。
它是**词法通道实际使用的口径**，写进凭证是为了让读者知道检索时用了哪种写法。

### 3.2 `variants[]`

```jsonc
{ "text": "…", "source": "verbatim|traditional|alias|verb|phrase", "rationale": "…" }
```

v0.1 只有词典驱动的三类改写（简繁、全称/简称/别称、公文套话与同义动词），
生成过程无随机、无采样、无模型。同一命题在任何机器上都得到同一组口径。

### 3.3 `corpus`

```jsonc
{
  "archive": "./council-notes",
  "docs": 7, "pages": 7, "parsed_pages": 7, "chunks": 21,
  "excluded": [ { "path": "scan.pdf", "reason": "no_text_layer", "pages": 3 } ],
  "manifest_sha256": "…"
}
```

- `pages` 是**已知页数**：已解析文件的页数 + 未解析文件里能读到的页数。
- `excluded[].pages == 0` 且 `reason == "encrypted"` 表示页数不可知（PDF 被口令封住时，
  连页树都读不出来）。台账会在标题句里单独点出这类文件的数量，不会把它们算成"0 页"。
- `excluded[].reason` 取值：`encrypted`、`no_text_layer`、`unsupported_type`、`parse_error`、`empty`。
  解析边界按文件类型硬编码，不存在"试一下、读不出就静默丢页"的路径。

### 3.4 `matrix`

```jsonc
{
  "chunks": 21, "variants": 3, "comparisons": 63,
  "sha256": "…",
  "scoring": {
    "lexical": "fts5_trigram_idf_term_coverage",
    "semantic": "cosine",
    "combination": "max"
  }
}
```

`comparisons = chunks × variants`，即全量笛卡尔积。**没有抽样**：句向量对每一个分块打分，
所以覆盖度天然是 100%，"缺席"的含义只能是"N×M 个分数全部低于 τ"。

`matrix.sha256` 的输入是每个 `(口径序号, 分块 key, 词法分, 语义分)` 四元组的定长文本行
（分数固定 6 位小数，按口径序号、再按分块 key 排序），前缀为 `absentia-matrix/1`。
任何一分变了，摘要就变。

### 3.5 `per_variant[]`

```jsonc
{
  "variant": "…", "chunks_scored": 21, "above_tau": 0,
  "top1_score": 0.11, "top1_chunk": "2293fbb64f:p0001:c000",
  "lexical_path": "fts_trigram|like_fallback|none",
  "fired_by": "lexical|semantic|both|none"
}
```

`lexical_path` 记录这条口径在词法通道上走的是哪条路：
FTS5 trigram 命中、2 字中文词走 `LIKE` 回退、还是完全没命中。
`fired_by` 说明最高分由哪个通道贡献——这是审阅时最该看的一列。

### 3.6 `bound`

```jsonc
{
  "embedding_model": "bge-m3",   // 词法-only 时为 null
  "tau": 0.62,
  "calibration_recall": 0.86,    // 随包发布的固定表，不是对本次语料的测量
  "lexical_only": false,
  "embedding_error": "ConnectError"  // 仅在降级时出现，类别名而非报错原文
}
```

`calibration_recall` 是**对阈值的陈述**，不是对本次档案的承诺：表内每个 τ 对应一个历史
召回估计，插值到不高于 τ 的最近档。凭证里写它，是为了让读者知道"τ=0.62 意味着什么"，
而不是让读者以为这次真的测了召回率——私有档案没有标注集，这一点无法回避。

### 3.7 `verdict`

```jsonc
{
  "label": "PRESENT|ABSENT_BOUNDED|INCONCLUSIVE",
  "reason": "score_at_or_above_tau|all_scores_below_tau|lexical_only|empty_corpus",
  "top1_score": 0.11, "top1_chunk": "…", "top1_variant": "…", "tau": 0.62
}
```

判定表（**只看数字，不看模型输出**）：

| 条件 | 判定 | `reason` |
| --- | --- | --- |
| 没有可打分的分块，或没有已解析页 | `INCONCLUSIVE` | `empty_corpus` |
| 最高分 ≥ τ | `PRESENT` | `score_at_or_above_tau` |
| 最高分 < τ 且本次只跑了词法通道 | `INCONCLUSIVE` | `lexical_only` |
| 最高分 < τ 且语义通道已覆盖全部分块 | `ABSENT_BOUNDED` | `all_scores_below_tau` |

第三行是产品的自我约束：字面没命中不等于缺席。没有可用嵌入端点时，其它写法没有被覆盖，
工具**拒绝**出具 `ABSENT_BOUNDED`，并在 `bound.lexical_only` 里自陈边界。

---

## 4. 打分

### 4.1 词法通道（`scan.lexical_scores`）

建表固定 `fts5(text, tokenize='trigram')`。这不是默认值而是硬约束：`unicode61` 会把一整段
中文切成一个 alnum token，查询「摄像头」命中 0 条，词法列等于常量漏检。trigram 之下，
三字及以上的词按子串命中；两字中文词（如「采购」）trigram 无法索引，走
`chunks.text_norm LIKE '%…%'` 回退，并记 `lexical_path: like_fallback`。

索引里存的是分块的**简体副本**（`text_norm`），引文与向量仍用原文，于是简繁任一侧的提问
都能命中另一侧的档案。

打分用 **IDF 加权口径覆盖率**，不用 BM25 原始分：

```
idf(t)      = ln(1 + N / (1 + df(t)))          # N=分块总数，df=命中该词的分块数
coverage(c) = Σ_{t 命中 c} idf(t) / Σ_{所有 t} idf(t)
gate(c)     = 1.0  若最高 idf 的词命中了 c
              0.5  否则
score       = coverage(c) × gate(c)
```

两条理由，都写在明面上：

1. **BM25 的量纲随语料漂移。** 同一句话在两万段的档案里 BM25 可能是 2，在二十段的档案里是 12；
   它无法与 cosine 共用同一个 τ，也就无法让不同档案的凭证互相比较。
2. **最高 idf 的词构成命题的特异性。** 问「Flock 摄像头采购」时，`Flock` 才是让问题成为问题的词；
   命中「摄像头」而没命中 `Flock`，只能算一半证据，所以折半。没有这道闸，任何含常用词的命题
   都会在无关分块上拿到高分。

分词是词典最长匹配（`variants.LEXICON_TERMS`）+ 虚词切分，未知长串整体保留，
不拆成单字。v0.1 不引入分词模型，也不联网取词表。

### 4.2 语义通道（`scan.request_embeddings`）

向本机 OpenAI 兼容端点（Ollama / llama.cpp）`POST {endpoint}/embeddings`，
按 32 条一批。不装 torch，不接任何托管 API。向量 L2 归一化后以 float32 blob 存进
`index.sqlite`，打分即点积，再截到 `[0, 1]`。

任一批次失败（端点不可达、返回条数不符、字段缺失）→ 整体降级为词法通道，
`bound.lexical_only=true`，`bound.embedding_error` 记异常**类别名**（不是报错原文，
否则路径与端口会让摘要不可复现）。不做部分覆盖：一半分块有向量、一半没有，是最危险的状态。

### 4.3 合并

`score = max(词法, 语义)`。两个通道是互相独立的证据来源，任一通道给出强证据即可；
加权平均会让"语义命中但词法为 0"的段落被稀释到阈值以下，恰好破坏 m2 验收 (b)。
`per_variant[].fired_by` 记录实际是哪个通道起的效。

---

## 5. 确定性

同一档案 + 同一命题 + 同一配置，两次运行必须逐字节一致。为此：

| 手段 | 位置 |
| --- | --- |
| 文件按相对路径排序；分块按文件内顺序 | `ingest.discover` |
| 分块 key = `文件内容 sha256 前 10 位:p页码:c序号`，与路径、时间无关 | `ingest._chunk_key` |
| 清单不含绝对路径、时间戳、文件 mtime | `ingest.build_manifest` |
| JSON 一律 `sort_keys`、无多余空白、`allow_nan=False`、UTF-8 | `ingest.canonical_json` |
| 分数固定保留 6 位小数，先取整再入摘要 | `scan.SCORE_DECIMALS` |
| 时钟只在信封，且尊重 `SOURCE_DATE_EPOCH` | `attest._created_at` |
| 排序与并列取首个（`>` 而非 `>=`），不依赖字典顺序 | `scan.run_scan` |

---

## 6. 复算（`absentia verify`）

```
absentia verify .absentia/attestation.json [--archive DIR] [--tau T] [--endpoint URL]
```

1. 先验信封：重算 `core` 的摘要，与 `core_sha256` 比对。不等即判"文件被改过"，
   直接返回，不再往下跑（改过判定的凭证不该得到"其它字段还好"的第二种说法）。
2. 在**临时目录**重建索引（不写被审档案，也不覆盖原凭证）。
3. 用凭证里的 `claim.text`、`bound.tau`、`bound.embedding_model` 重跑同一套流程。
4. 逐项比对：`manifest_sha256`、`matrix.sha256`、`verdict.label`、`verdict.top1_score`、
   `corpus.pages`。
5. 全部相等 → `AGREES`（退出码 0）；否则 `DISAGREES`（退出码 3），并列差异。

复算方默认只跑词法通道。若凭证记录了 `embedding_model` 而本次没有端点，
报告会带 `note` 提示补 `--endpoint`——此时通常会看到 `INCONCLUSIVE` 与 `ABSENT_BOUNDED`
的差异，这是配置差异，不是凭证错误，报告会原样列出。

---

## 7. 已知边界（v0.1）

- 不做 OCR：无文字层的扫描件进 `excluded`，页数计入"未复习"。
- 不支持增量索引：每次 `check` 全量重建（这是可复算的代价，也是刻意的选择）。
- 口径词典只覆盖简繁、全称/简称/别称、公文套话与同义动词，不覆盖方言与外语。
- `calibration_recall` 是随包固定表，不做自动校准曲线。
- 纯 Python 点积：万段级档案在普通笔记本上是分钟级，不是秒级；v0.1 不引入 numpy。
- 语义通道的可复算性依赖本机模型与端点本身确定；换量化版本会改变 `matrix.sha256`，
  凭证会把差异暴露出来，而不是假装一致。
