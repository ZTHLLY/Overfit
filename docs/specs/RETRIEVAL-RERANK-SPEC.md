# Retrieval Reranker 层 Spec

> 文档版本：0.5 · 日期：2026-10-28 · 状态：**P0～P9 已完成；用户显式授权的 OpenRouter smoke 与 47 题双轨评测均已通过**。
> 本文冻结 provider-neutral 行为、测试和评测边界；第一版只实现 OpenRouter adapter，并固定使用 `voyageai/rerank-3`。未来模型仍可通过配置替换，但不预写第二个 provider adapter、不自动调用真实 API。

实施顺序见 [RETRIEVAL-RERANK-PLAN.md](../plans/RETRIEVAL-RERANK-PLAN.md)。评测运行索引将位于 `docs/evaluation/RERANK-EVALUATION.md`；该文档只记录方法、基线、命令和 run 身份，不复制 JSON 数据。

## 1. 目标与非目标

目标是在现有 BGE-M3 dense retrieval 和 LLM 生成之间增加一层专用 cross-encoder/listwise reranker：

```text
query
  → BGE-M3 dense retrieval（最多 40 个候选）
  → 专用 reranker API（给全部实际候选打分）
  → 查询结果或 topic 多样性后处理（最多 10 个）
  → 必要时进入生成模型
```

本层应该：

- 提升正确 chunk 在 Top 10 内的顺序，而不只是继续扩大 dense candidate pool；
- 保留 vector 与 rerank 前后的分数和名次，可审计地回答“它改善了什么”；
- 使 provider 可替换，但第一版不预先写多个未使用的 adapter；
- 让正常产品路径 fail closed：已要求 rerank 就不允许失败后悄悄降级为 vector-only；
- 将网络行为、审计产物和真实评测变成用户可见、可重现的过程。

本批不做：

- 替换 BGE-M3、改变 1024 维索引或重建索引；
- 训练/微调 reranker，或用通用生成 LLM 伪装专用 reranker；
- 在没有 query 的 whole-course mock/coverage 上构造假 query；
- 根据 provider score 设置未校准的全局阈值；
- 将真实 API 调用放进默认 pytest、CI、后台任务或自动执行流程。

## 2. 触发规则：由 query 决定，不设 enable 开关

Reranker 是 query-time 能力，触发与否由请求本身决定，不新增 `RERANK_ENABLED`。

| 入口 | query | 第一版行为 |
| --- | --- | --- |
| `search QUERY` | `QUERY` | 必须 rerank |
| `mock --topic TOPIC` | `TOPIC` | 必须 rerank，然后做 topic 材料后处理 |
| `coverage --topic TOPIC` | `TOPIC` | 必须 rerank，与 topic mock 共用同一选材实现 |
| `mock` 无 topic | 无 | 不调 reranker，保留 clustering/allocation |
| `coverage` 无 topic | 无 | 不调 reranker，保留 clustering/allocation |
| `topics` | 无 | 不调 reranker |

有 query 的正常产品路径缺少 reranker key/provider/model 时，必须在任何 reranker 网络请求和生成请求之前当场报错，不运行、不默默退回 dense ranking。纯 vector baseline 只存在于明确的 evaluation/diagnostic 路径，不是产品 fallback。

Query 原样使用：第一版不自动拼接课程名、文件名、答案标签或隐藏 metadata。Query rewriting 若未来需要，必须作为独立可测层引入，不暗中混入 reranker。

## 3. 组件边界与可替换性

### 3.1 通用层

通用层定义 provider-neutral `Reranker` Protocol 和数据契约，不导入某家 SDK 类型。类名可在实现时微调，责任不变：

```python
class Reranker(Protocol):
    def rerank(
        self,
        query: str,
        documents: Sequence[RerankDocument],
    ) -> RerankResponse: ...

@dataclass(frozen=True, slots=True)
class RerankDocument:
    index: int
    text: str

@dataclass(frozen=True, slots=True)
class RerankScore:
    index: int
    score: float
```

Provider adapter 只负责请求/响应边界及外部错误的现象分类。Retriever/orchestrator 负责候选获取、最多三次尝试、响应校验、本地映射、多样性后处理和日志。Generator 只接收已选材料，不创建 reranker，不读 key，不决定重试，不通过 `if` 伪造 rerank 成功。

### 3.2 第一版 provider gate（已冻结）

- `provider`: `openrouter`
- `model`: `voyageai/rerank-3`
- `base_url`: `https://openrouter.ai/api/v1`
- endpoint: `POST /rerank`
- provider adapter 文件：`src/overfit/query/rerankers/openrouter.py`
- 认证：`Authorization: Bearer <OVERFIT_RERANK_API_KEY>`
- 默认 timeout：30 秒；总调用次数最多 3 次

第一版只新增这一家 adapter。更换 provider 应当只替换 adapter/configuration，不重写 Retriever、selection、logging 或 evaluation；切换 OpenRouter 已支持的 rerank 模型只改 model slug。Adapter 必须关闭 SDK 内部的隐式 retry，调用次数只由本项目 orchestrator 控制。

[OpenRouter Rerank API](https://openrouter.ai/docs/api/api-reference/rerank/create-rerank) 已确认请求包含 `model/query/documents/top_n`，响应包含有序 `results[].index`、`results[].relevance_score`，并可带 `id/provider/usage`。`top_n` 显式设为实际输入数 N，保证返回全部 N 个评分。Voyage 官方约束允许最多 1,000 documents、单 query-document 组合 32K、总请求 600K tokens；当前 Top 40 严格上界远低于限制。[`rerank-3`](https://openrouter.ai/voyageai/rerank-3) 当前标价为每百万输入 token 0.05 美元。上述 capability 已满足编码 gate；真实鉴权、账户额度和端到端响应仍由用户显式 live smoke 验证。

### 3.3 执行状态

Rerank 流程使用三态，而不是无来由的单独 bool：

| 状态 | 含义 | 后续行为 |
| --- | --- | --- |
| `APPLIED` | 已获得并完整验证 rerank 响应 | `success == true`，可继续 |
| `SKIPPED` | 该路径本来就没有 query | 继续现有 no-topic 流程 |
| `FAILED` | 有 query 但配置、候选、请求、响应或日志不满足契约 | 终止，不进入生成 |

`success` 是 `status == APPLIED` 的派生属性，不单独存一份可与状态矛盾的事实。有 query 但没有任何可用候选时不发空 API 请求，记为 `FAILED/no_candidates`；生成路径不继续。

## 4. 候选、打分和最终选择

### 4.1 计数语义

`40 → 10` 是默认且有明确层次的上限：

- dense candidate pool 最多 40；
- reranker 对实际候选集中的每一项返回 score，不只要 provider Top 10；
- search 默认返回 rerank Top 10，不做 source cap 或长度过滤；
- topic mock/coverage 在 rerank 后扫描全部实际候选，经多样性约束选最多 10 个；
- 若命令已有 material/top-k 预算，它只能将最终上限调低或显式覆盖成用户要求的值，不能让 provider 只返回前几个分数而破坏审计。

Storage API 必须把完整 candidate pool 交给 Retriever。当前 `VectorStore.search()` 即使接收 `fetch_k`也最后截成 `top_k`，实现时必须修正或新增语义明确的 candidate-search 接口，不能把只有 10 个的列表冒充 Top 40。

### 4.2 Topic 预过滤

Search 要允许用户找到短定义/标题，因此不套用生成材料的长度门槛。

Topic mock/coverage 需要能支撑出题的长材料：先取 dense Top 40，再在 API 前排除空白、纯标题和少于 200 字符的低信息 chunk。过滤后的实际数量为 `N ≤ 40`，一次发送这 N 个；不从 dense 第 41 名以后补数。如果因过滤而少于 10，不把低质量 chunk 填回去。

### 4.3 分数与名次不覆盖

现有含糊的单一 `RetrievedChunk.score` 不能继续在 rerank 时被覆盖。内部结果至少明确保留：

```python
vector_score: float
vector_rank: int
rerank_score: float | None
rerank_rank: int | None
```

迁移旧调用方时不得新增另一个含糊的“final score”冒充两个不同量表。Provider score 只在相同 provider/model/protocol 内用于排序和分析，不设置跨 provider 的统一阈值。第一版不写 `rerank_score >= X` 规则；待收集真实分布后再决定。

### 4.4 响应完整性

输入数量为 N 时，成功响应必须包含恰好 N 个结果：

- 每个 input index 恰好出现一次；
- index 在 `[0, N)` 内，无重复、缺失或额外项；
- score 是有限数，拒绝 NaN/Infinity/非数值；
- 只按冻结的输入位置映射回本地 chunk，不依赖 provider 回显文本或 metadata；
- 本地按 score 降序产生 `rerank_rank`，同分时使用原 `vector_rank` 稳定破局。

任一响应契约违反都属于第 6 节的“其他错误”：立即 `FAILED`，不将部分结果投入生成，也不因返回分数低而重试。

### 4.5 Topic 多样性

仅 topic mock/coverage 在 rerank 后做以下顺序扫描；search 保留纯 rerank 相关性顺序。

1. 文本 Unicode/空白规范化后完全一致，视为重复；
2. 现有本地 BGE-M3 embedding 余弦相似度 `>= 0.92`，视为语义近重复；
3. 已选集中同一 `Chunk.source` 最多 2 个；source 指完整课程相对文件路径，不是 week 目录；
4. 从 rerank 第 1 名起扫描，跳过违反上述约束的项，直到达到最终预算或候选耗尽；
5. 候选耗尽仍不足 10 时返回较少材料，不放宽规则。

每个未入选项记录单一明确原因：`low_information`、`exact_duplicate`、`near_duplicate`、`source_cap` 或 `outside_final_limit`。最终材料按 rerank 优先级传给生成模型，不再按 source/page 排序而丢掉相关性顺序。

## 5. 外部请求与配置

### 5.1 最小外发 payload

OpenRouter 只接收完成排序所必需的内容：

```json
{
  "model": "voyageai/rerank-3",
  "query": "<query text>",
  "documents": ["<chunk 0 text>", "<chunk 1 text>"],
  "top_n": 2
}
```

`top_n` 始终等于实际候选数 N；adapter 使用冻结数组位置作为本地 index。OpenRouter 响应可能回显 document，adapter 不依赖或记录回显文本，只使用 `index/relevance_score`，并按可得情况记录非敏感 `id/provider/usage`。不外发 source、page、chunk ID、文件路径、整份 PDF、整个索引、golden label 或其他课程元数据。

第一版禁止静默拆 batch、合并不可比 score，也禁止为迁就上下文而静默截断 chunk。Provider-specific adapter 如能在请求前确定超限，应 fail fast。

### 5.2 当前 token 容量证据

对当前两个 IFN580 索引的只读审计显示：

- 1,385 chunks，BGE-M3 1024d，`chunk_size=250`；
- 单 chunk 字符数 P50=370、P95=985、max=1,053；
- Top 40 文档文本的严格字符上界是 42,120，以 4 chars/token 粗略估算约 10,530 document tokens；
- 即使按 query 对 40 个 document 重复计入的保守估算，当前 question+options 最坏也不到 20K tokens。

该审计与已核对的 Voyage/OpenRouter 官方限制共同支持单请求 Top 40；live 运行仍记录 provider 实际 usage，不把字符估算冒充精确计费 token。

### 5.3 配置和 CLI 覆盖

默认从环境变量读取，同时保留 CLI UX：

```text
OVERFIT_RERANK_PROVIDER=openrouter
OVERFIT_RERANK_BASE_URL=https://openrouter.ai/api/v1
OVERFIT_RERANK_API_KEY=<local secret>
OVERFIT_RERANK_MODEL=voyageai/rerank-3
OVERFIT_RERANK_TIMEOUT=30
```

第一版只接受 `provider=openrouter`；显式字段用于配置审计和未来替换，不代表已实现多 provider registry。命令行使用明确的 reranker 前缀：

```text
--rerank-provider
--rerank-api-key
--rerank-model
--rerank-base-url
--rerank-timeout
```

同一字段的优先级是 CLI > environment > code default；provider/model/base URL/timeout 使用本节冻结值作为默认，API key 没有默认值。API key 不得进入 stdout/stderr、JSON、Markdown、exception repr、URL、trace 或 cache key；base URL 记录前移除 userinfo/query/fragment。日志仅保留非敏感 provider/model/endpoint identity。

## 6. 三档现象错误模型

错误处理不维护一份不可能穷举完的 HTTP 状态码清单，只根据可观察现象分三档：

| 档位 | 可观察现象 | 行为 |
| --- | --- | --- |
| 外部请求未成功 | timeout、连接/DNS/传输错误，或任意 non-2xx HTTP 响应 | 总共最多 3 次调用 |
| 主动 shutdown | Ctrl+C、进程/上层取消 | 立即上抛，不重试 |
| 其他 | 本地配置/输入、HTTP 2xx 后的响应契约、数据映射、日志、代码等错误 | 立即失败，不重试 |

因此 401、429 和 5xx 在第一版里都属于“外部请求未成功”，最多三次；新 provider 状态码不需要不断加入中央列表。HTTP 2xx 但返回缺失/NaN/重复 index 是本地契约错误，按最新决策立即失败，不重试。

重试契约：

- 总尝试数最多 3（初始 1 次 + 最多 2 次重试）；
- 每次默认 timeout 30 秒；
- 重试前默认等待约 1 秒、2 秒，可加少量 jitter；
- provider 给出较短 `Retry-After` 时可使用，但不允许无上限延长命令；
- 三次仍未获得 2xx 响应时记 `FAILED`，返回非零退出码，停止当前 search/mock/coverage/eval 进度；
- 分数低、排名不理想或未达评测目标绝不是重试理由。

## 7. 审计日志与终端 UX

### 7.1 机器记录

本地原始记录放在已被 Git 忽略的输出树：

```text
outputs/rerank/
  runs/
    <timestamp>-<command>-<run_id>.json
  cache/
    <request_digest>.json       # 仅 eval 可用
  latest.json
```

每个 run JSON 至少记录：

- schema/protocol version、run ID、命令、时间和最终状态；
- provider、model、脱敏 endpoint identity、timeout、请求次数；
- 本地 query 原文与 hash；
- 有序 chunk ID/text hash、vector score/rank、rerank score/rank；
- 是否入选、最终位置、drop reason；
- 每次尝试的开始/结束时间、latency、HTTP status（若有）、三档错误类别和 provider request ID（若有）；
- provider 返回的 usage/cost 信息；没有则为 null，不猜测价格。

日志不重复保存 chunk 全文；本地 chunk ID 和 text hash 用于身份与 cache key。不保存 API key、Authorization header、完整原始 SDK 异常或内部 reasoning。`docs/evaluation/RERANK-EVALUATION.md` 只索引 run ID/相对路径、基线、命令、结论和局限，不人工复制 JSON 表格。

### 7.2 先记录、再调用、记录失败则 fail closed

1. API 调用前先创建 run 目录/文件并持久化初始身份、冻结请求摘要和 attempt intent；此步失败则零 reranker 调用。
2. 更新 JSON 时先在同目录写临时文件，flush/fsync 后原子 rename；不引入数据库或多文件交易。
3. Reranker 成功后，必须先成功写入完整分数/选择记录，才允许生成模型启动。
4. 调用已发生但日志失败时，stderr 说明 reranker 已调用、generation 未开始，命令失败且返回非零。不因事件小概率而假称能将失败写回同一个故障介质。
5. `latest.json` 是派生便利入口，run 文件才是该次执行的事实源；第一版仍将 latest 更新视为必需日志步骤，它失败时即使 run JSON 完整也返回非零并且不启动 Generator。

### 7.3 用户可见状态

CLI/orchestration 层将简明状态写到 stderr，不污染 stdout 的结构化/正式输出：

```text
[reranker] applied provider=... model=... candidates=N selected=K attempts=A latency_ms=...
[reranker] skipped reason=no_query
[reranker] failed attempts=A; generation aborted
```

有 query 的生成路径只有在 `APPLIED` 且完整日志已持久化后才调 Generator。这个 `if` 属于 CLI/Retriever orchestration，不放进 Generator 内部。

## 8. 评测、缓存与可重现性

### 8.1 两条独立评测轨道

同一批 47 道 golden questions 分别运行，结果绝不混算：

1. **question-only**：严格诊断 reranker 仅依据题干理解 query/material 的能力；
2. **question+options**：模拟选项可用的最终产品路径，是未来是否采用该 reranker 的主要产品依据。

两条轨道使用同一 dense Top-40 规则、相同 provider/model/config 和固定 golden 版本。不因某组数字更好看而临时更换主指标。

当前 dense baseline 作为参考：

| 轨道 | Hit@1 | Hit@3 | Hit@5 | Hit@10 | MRR@10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| question-only | 21/47 = 44.7% | 35/47 = 74.5% | 39/47 = 83.0% | 42/47 = 89.4% | 0.614 |
| question+options | 66.0% | 85.1% | 93.6% | 95.7% | 0.773 |

每次报告 Hit@1/3/5/10、MRR@10、单题 rank delta、API 成功率、尝试次数、P50/P95 latency 及 provider 能返回的 usage/cost。API 失败不从分母悄悄删除；运行中止时记 incomplete，后续由用户显式恢复。

### 8.2 软目标，不是调到过拟合的停止线

Question-only 的首轮参考目标为 Hit@1 约 `>=52%`、Hit@3 约 `>=80%`、MRR@10 约 `>=0.67`，且 Hit@10 不出现明显下降。这些是解读结果的期望，**不是代码必须调到过线才能停止的验收门**。

工程完成取决于全量运行/失败记录、可比指标和可重现产物，不取决于挑参达线。只要主要指标出现可信提升，可判定方向有效；持平或下降也必须原样报告，不重试、换分母或只展示有利 query。

### 8.3 Eval-only cache 和断点

正常 search/mock/coverage 第一版不使用隐藏 rerank cache。独立 eval 可用 cache key：

```text
protocol_version
+ provider + model
+ exact query hash
+ ordered candidate IDs and text hashes
+ rerank configuration
```

Cache hit 必须再经当前 schema/完整性校验，并记录 `from_cache=true`、实际 API calls=0。提供 `--no-cache` 强制重测。评测默认 `concurrency=1` 顺序执行，不在未知 rate limit 时并发请求。中途失败保留已完成项和下一个待执行身份，但恢复必须由用户显式发起，不后台自动继续。

默认 pytest 和 CI 绝不访问真实 reranker。Live integration/eval 必须有明确 live/execute 标志、完整配置，并由用户本人在电脑前发起。没有 key 或明确 live 意图时跳过/拒绝，不由 agent、fixture、import side effect 或自动化调用。

## 9. 测试边界

离线测试至少分为：

1. **纯函数/单元测试**：fake reranker 覆盖 APPLIED/SKIPPED/FAILED、Top 40/10、空候选、稳定 tie、去重、source cap、长度过滤和分数保真；
2. **retry 测试**：timeout、transport、non-2xx 前两次失败第三次成功，三次全失败，shutdown 不重试，2xx 契约错误不重试；
3. **provider contract 测试**：使用伪造 HTTP/SDK 响应验证最小 payload、配置映射、错误现象和 N 个结果；
4. **I/O 故障注入**：初始 intent 写入失败不调 API，成功响应后日志失败不调 Generator，临时文件/原子替换不把半个 JSON 当完整 run；
5. **CLI 测试**：key CLI > env，有 query 无 key fail fast，no-topic 无 key 正常，stderr 状态与非零退出码正确；
6. **eval 测试**：两轨道分开、cache key 完整、`--no-cache`、断点恢复和失败分母正确；
7. **live integration**：默认 skip，只在用户显式开启时运行，不纳入默认 pytest 通过条件。

默认测试必须在断网、无 reranker key 环境中可重复通过。作者测试通过后由独立 reviewer/verifier 执行审查和回归，不以真实 API 一次成功替代工程证据。

## 10. 验收标准

| ID | 验收要求 |
| --- | --- |
| **RR-01** | 只有真实 query 路径调 reranker；no-topic mock/coverage/topics 必须 SKIPPED 且零 reranker 调用；不存在 enable 开关。 |
| **RR-02** | 有 query 而 key/provider/model 缺失时在外部/生成调用前 fail fast；CLI `--rerank-api-key` 覆盖 env，secret 不落盘/回显。 |
| **RR-03** | Dense candidate pool 上限 40、最终上限 10；topic 先从 Top 40 过滤低信息文本并发送实际 N，不从第 41 名补数。 |
| **RR-04** | 每个候选的 vector score/rank 和 rerank score/rank 分开保留，无单 score 覆盖；完整记录可重现 rank delta。 |
| **RR-05** | 成功响应对 N 个输入返回恰好 N 个唯一、范围内 index 和有限 score；同分用 vector rank 稳定排序；契约错误立即失败。 |
| **RR-06** | Topic 顺序扫描实现规范化精确去重、embedding cosine `>=0.92` 近去重和每 `Chunk.source` 最多 2 个；不足 10 不补垃圾。 |
| **RR-07** | 三档现象模型生效：外部未成功最多 3 次，shutdown 立即停，其他立即失败；默认 30s timeout 及约 1s/2s 退避不被 SDK 隐式 retry 突破。 |
| **RR-08** | 外发仅 `model/query/documents/top_n`，其中 documents 只有 ordered chunk texts；不发 source/page/ID/PDF/index/golden；不记 key/header/reasoning/原始敏感异常。 |
| **RR-09** | `outputs/rerank` JSON 在调用前初始化、原子更新并记录尝试/分数/去留；必需日志失败则非零且不进入 Generator；stderr 明示 applied/skipped/failed。 |
| **RR-10** | Provider-neutral Protocol 与编排逻辑分离；第一版只有用户选定的一个 adapter；更换 reranker 不要求重建 BGE-M3 索引。 |
| **RR-11** | 默认 pytest/CI 使用 fake 并且零真实 API；live integration/eval 只能由用户带明确 live/execute 意图发起。 |
| **RR-12** | 47 题 question-only 与 question+options 分开运行/报告；eval 默认顺序，cache key 包含完整请求身份，支持 `--no-cache` 和用户显式断点恢复。 |
| **RR-13** | 同时报告 Hit@1/3/5/10、MRR@10、rank delta、成功率、延迟和可获 usage/cost；52%/80%/0.67 等只作软目标，未达标不诱发无限调参。 |
| **RR-14** | search/mock/coverage 的 query/no-query 分支、退出码、stderr、最终顺序及现有 no-topic 行为均有回归证据；旧索引可继续使用。 |

## 11. 延期项与实现检查点

Provider/model/endpoint/认证方式已冻结。以下仍需在实现或用户显式 live 阶段验证，不靠文档猜测：

- 用户账户的长期 rate limit；P9 的 94 次顺序请求无限流/失败，但不把一次完整运行外推为账户通用限额；
- 当前实现使用项目内显式 HTTP transport，调用次数由本项目 orchestrator 控制；不启用 SDK 隐式 retry；
- provider tokenizer/context 的精确本地预检实现；
- 基于真实 score 分布的阈值、校准或更换去重阈值；
- 并发、产品路径隐式 cache、多 provider fallback 和 batch score normalization。

P0～P9 已完成：编码门、离线 fake/contract tests、用户显式授权的真实 smoke 和 47 题双轨评测均已通过。

## 12. 当前实施状态与证据

P1～P7 已按本规范实现并由作者以外的 code reviewer 和 verifier 独立批准。离线证据为：目标测试 `100 passed`、全量回归 `723 passed`（独立复跑约 2.9 秒）、Ruff 全绿、`git diff --check` 通过。验证显式覆盖空 `OVERFIT_RERANK_API_KEY`，未读取/输出本机 secret，未联网或调用真实 reranker。

独立复核发现的懒配置校验、trace override identity、调用前 attempt intent、非法 2xx attempt 状态、失败 attempts 计数，以及 cache endpoint/ordered candidate ID 身份问题，均在批准前修复并回归。

RR-12 的双轨 query、exact-request cache、`--no-cache`、顺序 runner、显式 resume 和 `from_cache` 审计均已实现并离线验收。RR-13 的 47 题指标、成功率、延迟、usage/cost 和软目标解读已由 P9 完成。

P8 已由用户显式授权并成功执行两次真实调用：

1. Synthetic query + 3 documents：`attempts=1`；Paris rerank rank 1、score `0.892326`，Berlin rank 2，banana rank 3。记录位于 `outputs/rerank/runs/20261028T070403Z-live-smoke-synthetic-f78f1628-7633-475b-bc60-14a5c209297e.json`。
2. IFN580 week01 question-only Top-40→10：run ID `07752160-2933-4b31-87dc-3a9e119ba33b`；candidate budget/actual `40/40`，final/selected `10/10`；`attempts=1`，latency `1652.382 ms`，`total_tokens=6172`，cost `$0.0003086`。Top 1 为 `week1/introduction.pdf p13`（vector rank 2 → rerank rank 1），Top 2 为同文件 p21（vector rank 1 → rerank rank 2）；search selection policy 记录 `applied=false`。

两份 P8 日志均没有 `api_key` 或 `authorization` 字段，且未调用生成模型。

P9 已由用户显式授权，使用与正式 dense baseline 完全相同的 golden 与索引快照：dataset SHA-256 `60d1455702a17df25119d4232b3ee2c95cda493cb255f4811d14de0107cb5a5f`，index SHA-256 `668fb3b946af45c0a73da1d398a245767d2f20a20d605d54358410c610b34e4a`。两轨各 47 题、`concurrency=1`、强制 `--no-cache`，94 次真实 API 调用均一次成功：`success/failure/cache=94/0/0`、`attempts=94`、成功率 `1.0`、latency P50/P95 `945.7/1385.191 ms`、`total_tokens=683962`、cost `$0.0341981`。

P9 runner 上线前通过全量 `731 passed`、Ruff 和 `git diff --check`；独立 reviewer 复验 63 个目标测试，并确认 47×2 条 query 与正式 dense baseline 逐字节一致。

| 轨道 | Dense H@1/3/5/10 | Rerank H@1/3/5/10 | Δ H@1/3/5/10 | Dense → Rerank MRR@10（Δ） |
| --- | --- | --- | --- | --- |
| question-only | 0.446809 / 0.744681 / 0.829787 / 0.893617 | 0.595745 / 0.893617 / 0.936170 / 0.936170 | +0.148936 / +0.148936 / +0.106383 / +0.042553 | 0.614421 → 0.744681（+0.130260） |
| question+options | 0.659574 / 0.851064 / 0.936170 / 0.957447 | 0.744681 / 0.957447 / 0.978723 / 0.978723 | +0.085107 / +0.106383 / +0.042553 / +0.021276 | 0.773286 → 0.849291（+0.076005） |

JSON 事实源与 Markdown 报告分别位于 `outputs/eval/rerank-20261028T072026Z-4a90e805-c4c9-40d5-94e1-3040e67ae23d/results.json` 和同目录 `report.md`，长期索引见 `docs/evaluation/RERANK-EVALUATION.md`。评测产物不含 `api_key`、`authorization` 或 bearer 值；本轮未调用 Generator 或 judge。两轨的 Hit@1/3/5/10 和 MRR@10 均为正向变化，因此在当前固定 47 题实验上，reranker 方向有效；不将该结论外推到未见题或生成/judge 质量。
