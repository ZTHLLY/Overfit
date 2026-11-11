# Retrieval Reranker 实施计划

> 文档版本：0.5 · 日期：2026-10-28 · 状态：**P0～P9 已完成；用户显式授权的 OpenRouter smoke 与 47 题双轨评测均已通过**。
> Key 只留在本机环境，不写入本文、日志或 Git；本轮未 commit/push。P9 已由用户本人在电脑前显式发起。

行为规范以 [RETRIEVAL-RERANK-SPEC.md](../specs/RETRIEVAL-RERANK-SPEC.md) 为准。本计划组织实施顺序、改动落点和验证证据；若计划与 spec 冲突，以 spec 为准。

## 1. 交付边界与执行顺序

目标路径：

```text
有 query
  → dense retrieval（最多 40 个候选）
  → 必要的 topic 低信息过滤
  → 单次专用 reranker API（返回每个输入候选的 index + score）
  → topic 路径近重复/来源约束
  → 最多 10 个或调用方明确预算
  → generation

无 query
  → 保持现有 clustering / allocation / spread
  → 不创建 reranker、不校验 reranker key、不发送外部请求
```

实施顺序固定为：

1. **P0 provider gate（已完成）**：冻结 OpenRouter + `voyageai/rerank-3`，核对官方请求、响应、计费和限制。key 只作为后续 live gate。
2. **P1 契约测试先行**：先写 fake/contract tests，冻结状态、分数、响应和失败语义。
3. **P2 数据与候选池**：保留 vector/rerank 两套 score/rank，修正 `fetch_k` 只返回 `top_k` 的现状。
4. **P3 reranker 核心和唯一 provider adapter**：抽象可替换，首版只实现一个真实 provider 文件。
5. **P4 query/topic 接入**：search、topic mock、topic coverage 共用同一编排；无 query 行为不变。
6. **P5 CLI 和配置**：fail-fast、用户可见状态、CLI/env 覆盖、禁止静默降级。
7. **P6 运行日志和 schema**：调用前预写、原子 JSON 更新、失败即停、离线可审计。
8. **P7 离线回归与独立复核**：默认测试无网络、无真实 API、无费用。
9. **P8 用户显式 live smoke test**：只在用户本人发起后进行最小真实调用。
10. **P9 47 题评测**：顺序执行、缓存/断点、question-only 与 question+options 分开报告。

P0 已通过；实现只写经过核对的 `openrouter.py`，provider-specific 细节只在该 adapter 内出现。

## 2. 实施前事实与改动落点

| 落点 | 当前事实 | 计划责任 |
| --- | --- | --- |
| [models.py](../../src/overfit/models.py) | `RetrievedChunk` 只有会被覆盖的单个 `score` | 显式保留 `vector_score/vector_rank/rerank_score/rerank_rank`，消除分数含义歧义 |
| [store.py](../../src/overfit/storage/store.py) | SQL 会取 `max(fetch_k, top_k)`，但最终仍切成 `results[:top_k]` | 让上层真正取得完整候选池；冻结 `top_k/fetch_k` 语义并覆盖回归 |
| [retriever.py](../../src/overfit/query/retriever.py) | `retrieve()` 仅 dense search；topic 路径是 dense pool → MMR；无 topic 走 clustering | 在有 query 的两条路径接入统一 rerank orchestration；无 query 路径不变 |
| [selection.py](../../src/overfit/query/selection.py) | 现有 MMR 使用 vector relevance，会推翻 reranker relevance；`_substantial()` 未用于 topic 路径 | topic rerank 前过滤低信息文本；rerank 后只做近重复和 source cap，不再用旧 vector relevance 覆盖排序 |
| [config.py](../../src/overfit/config.py) | 没有独立 reranker 配置 | 新增 `RerankSettings`；不增加 `RERANK_ENABLED` |
| [cli.py](../../src/overfit/cli.py) | search 默认 top-k 5；mock/coverage 不识别 reranker key；生成层直接接收 chunks | query 命令在打开 store/调用 embedding 前验证配置；CLI 编排打印 stderr 状态，generator 不感知 reranker |
| [errors.py](../../src/overfit/errors.py) | 没有 reranker 领域错误 | 新增少量内部错误/终态，服务于三档现象模型，不维护 provider 状态码枚举 |
| [evaluation/live.py](../../src/overfit/evaluation/live.py) 及 trace/contracts | 当前 generation trace 只记录最终材料，无法审计候选重排 | 版本化记录或增加独立 rerank run 记录；旧 trace/replay 行为不静默改义 |
| `outputs/` | 已由 `.gitignore` 排除，仅保留 `.gitkeep` | 运行 JSON 放入 `outputs/rerank/`；代码和测试不得提交真实 key 或真实响应文本 |

已完成的规模审计：两个现有索引均为 1,385 chunks、BGE-M3 1024d；单 chunk 最大 1,053 字符。Top-40 文档严格上界约 10,530 tokens；即使按 query 对 40 个文档重复计数，现有 question+options 最坏估算仍不到 20K tokens。已选 [`voyageai/rerank-3`](https://openrouter.ai/voyageai/rerank-3) 支持当前规模，OpenRouter 统一 [`POST /api/v1/rerank`](https://openrouter.ai/docs/api/api-reference/rerank/create-rerank) 返回 index/score，因此首版不拆批。

## 3. P0：OpenRouter / `voyageai/rerank-3` gate（已完成）

- [x] 用户明确选择 `provider=openrouter`、`model=voyageai/rerank-3`；编码阶段不要求 key。key 只在用户显式发起 live smoke/eval 时从本机 CLI/env 提供，不写入 spec、plan、测试 fixture、日志或 Git。
- [x] 官方 API 已冻结：`POST https://openrouter.ai/api/v1/rerank`，Bearer 认证，请求使用 `model/query/documents/top_n=N`，响应读取 `results[].index/relevance_score`。
- [x] 当前 Top 40 规模满足 Voyage document/context/总 token 限制；OpenRouter 当前模型页标价为 `$0.05/M` input tokens。账户实际额度和 rate limit 留待 live 验证。
- [x] 本地使用冻结输入数组 index 映射回 chunk；OpenRouter 即使回显 document 也不依赖、不记录该副本，按可得记录 `id/provider/usage`。
- [x] 配置冻结为 `OVERFIT_RERANK_PROVIDER=openrouter`、`OVERFIT_RERANK_BASE_URL=https://openrouter.ai/api/v1`、`OVERFIT_RERANK_MODEL=voyageai/rerank-3`、`OVERFIT_RERANK_TIMEOUT=30`，key 保持本机 secret；CLI 使用对应 `--rerank-*` 覆盖。
- [x] live smoke 形状已定义：先用合成 query/documents 验证鉴权和 schema，再做一条真实 Top-40；**不会由自动测试或代理自行发起**。
- [x] 用户已在本机配置 key 并显式授权执行 P8 live smoke；真实鉴权、合成请求和一条 IFN580 Top-40 请求均成功。

**完成定义：** 编码 gate、P8 真实鉴权/端到端响应与 P9 固定双轨评测均已通过；质量结论仅适用于当前固定评测。

## 4. P1：契约测试先行

先新增失败测试，再进入实现。默认 `pytest` 必须在禁止网络的环境中通过。

建议测试落点：

- `tests/test_reranker.py`
- `tests/test_rerank_selection.py`
- `tests/test_rerank_logging.py`
- `tests/test_rerank_cli.py`
- `tests/test_retrieval_eval.py`
- `tests/test_openrouter_reranker.py`

必须先覆盖：

- [x] `APPLIED / SKIPPED / FAILED` 三态及派生 `success`；无 query 是 `SKIPPED`，不是失败。
- [x] 有 query 但缺 key/config 在打开 index、调用 embedding 或网络前失败；无 query 不读取/验证 reranker 配置。
- [x] 单次提交实际候选数 `N ≤ 40`；响应必须恰有 N 个唯一、范围内 index 和有限 score。
- [x] score tie 以原 vector rank 稳定破局；输入顺序不能因 provider 回显文本改变。
- [x] 任意 timeout、transport error 或 non-2xx HTTP 属于“外部请求未成功”，总尝试最多 3 次；默认每次 30 秒，两次退避约 1 秒/2 秒。
- [x] Ctrl+C/取消/主动 shutdown 立即传播，不重试。
- [x] 其他错误（配置、2xx 非法响应、映射、日志、代码错误）立即失败，不重试。
- [x] 三次失败后 exit code 非零，不调用 generator；不存在静默 dense fallback。
- [x] 默认测试使用 fake client/伪 HTTP 响应，断言真实 client 未构造、socket 未连接。
- [x] live integration test 默认 skip，必须同时有显式 execute 开关和本机 key 才可进入；CI 和普通 `pytest` 不得触发。

**完成定义：** 不实现 adapter 也能从测试读出全部边界；失败测试证明旧代码尚未满足契约。

## 5. P2：数据模型和候选池语义

- [x] 将 `RetrievedChunk` 迁移为显式的 vector/rerank 分数与名次，不再用 `with_score()` 覆盖 dense 证据。
- [x] 冻结兼容策略：更新所有构造点、序列化和测试；若保留 `score` 属性，只能是有文档定义的只读兼容视图，不能成为第二份可变事实。
- [x] dense search 返回稳定 `vector_rank`（1-based）和 `vector_score`；相同分数保持数据库/vector 顺序可复验。
- [x] 修正 `VectorStore.search()` 的 `fetch_k` 行为，或拆出名称清晰的 candidate API；调用者请求 40 时必须实际收到最多40而非10。
- [x] 不修改索引 schema、embedding、chunk text 或 BGE-M3 向量；更换 reranker 不要求 reindex。
- [x] 为旧 search、no-topic mock/coverage、trace 序列化和 provenance roundtrip 补回归测试。

**完成定义：** 同一候选的 dense 与 rerank 两套证据都可读取，store 候选池宽度有单测证明，原索引无需迁移。

## 6. P3：抽象层、唯一 Provider Adapter 与重试

建议模块边界（名称可按项目风格微调，但职责不得混回 CLI/generator）：

```text
src/overfit/query/rerankers/
├── base.py             # Reranker Protocol、输入/结果/状态契约
├── service.py          # 三次尝试、三档现象、稳定映射与编排
└── openrouter.py       # 第一版唯一真实 provider adapter
```

- [x] `Reranker` Protocol 只描述 query + 有序候选 → 有序评分结果；不暴露 SDK 类型给 retriever。
- [x] 首版只创建 `openrouter.py`，不预写第二家空 adapter，不设计运行时 provider registry。
- [x] 外发 payload 仅含 `model/query/documents/top_n=N`；documents 是按冻结顺序排列的 chunk 文本，source/page/chunk_id/golden label 留在本地。
- [x] 一次请求提交全部 N 个候选，不拆批、不合并跨请求不可比分数。
- [x] 2xx 响应严格校验完整、唯一、范围内 index 和 finite score；非法即第三档立即失败。
- [x] 本地按 score 降序稳定排序，tie 用 vector rank；保留 provider 原 score，不做未校准归一化或通用阈值。
- [x] 重试器只看现象：timeout/transport/non-2xx 最多三次；shutdown 立即传播；其他立即失败。不维护一张不断增长的状态码行为表。
- [x] 如响应含合理 `Retry-After`，可在总等待约束内采用；否则退避约 1 秒、2 秒。
- [x] API 返回 usage/cost/request-id 时以非敏感字段记录；不假造 provider 未返回的费用。

**完成定义：** fake-client 覆盖全契约，provider contract tests 验证真实 schema 映射；默认测试零网络。

## 7. P4：Search、Topic Mock/Coverage 与后处理

### 7.1 触发规则

- [x] `search QUERY`、`mock --topic QUERY`、`coverage --topic QUERY` 默认且必须 rerank；不存在 `RERANK_ENABLED`。
- [x] no-topic `mock`、`coverage` 和 `topics` 保持现有 clustering/allocation/spread，永不调用 reranker。
- [x] 纯 dense baseline 只存在于明确 eval/diagnostic 路径，不是普通 query 命令的隐式回退。

### 7.2 候选与最终数量

- [x] search 最多提交 dense Top 40，不按文本长度过滤；默认最终 10，合法显式覆盖受候选上限约束。
- [x] topic mock/coverage 先取 dense Top 40，再删除空白、纯标题和 `<200` 字符低信息材料；不从第41名以后补数，提交剩余 N≤40。
- [x] topic mock 默认最终材料预算10，显式 `--material` 覆盖；no-topic mock 保持 `material or questions * 2`。
- [x] coverage 保留显式 `--count` 作为最终展示预算，并复用同一 topic rerank/后处理实现。
- [x] 候选不足或过滤后不足时返回较少结果，不用垃圾 chunk 补齐。

### 7.3 Topic 多样性约束

- [x] rerank 后按 score 从高到低顺序扫描，达到预算即停止。
- [x] 规范化文本完全相同直接去重；已有 BGE-M3 embedding 余弦相似度 `≥0.92` 视为近重复，保留 rerank 更高者。
- [x] 每个精确 `Chunk.source` 相对路径最多保留2个；不按整个 week 文件夹限额。
- [x] 被排除候选记录 `low_information / exact_duplicate / near_duplicate / source_cap / outside_final_limit` 原因。
- [x] 不再调用现有以 vector relevance 为主项的 MMR 推翻 rerank 顺序；可复用纯 similarity 工具，不复用错误的 ranking semantics。
- [x] 传给 generator 的 chunk 保持最终 rerank 优先级，不重新按 source/page 排序。

**完成定义：** search 最多对40个候选重排，topic 对 Top 40 预过滤后的全部实际 `N≤40` 候选重排；无 query 路径逐项回归不变；所有选入和淘汰均可解释。

## 8. P5：配置、CLI UX 与失败可见性

- [x] `RerankSettings` 提供 provider、key、model、base URL、timeout；默认冻结为 `openrouter`、`voyageai/rerank-3`、`https://openrouter.ai/api/v1`、30 秒和总尝试3次，key 无默认值。
- [x] key 支持环境变量 `OVERFIT_RERANK_API_KEY` 和 CLI `--rerank-api-key`；优先级 `CLI > environment/.env > missing`。
- [x] provider/model/base URL/timeout 可由 `OVERFIT_RERANK_*` 配置，并支持对应 `--rerank-*` CLI override；第一版 provider 非 `openrouter` 时明确拒绝而非假装存在其他 adapter。
- [x] CLI key 不回显、不进入 exception repr、日志、trace、缓存键原文或测试 snapshot。
- [x] query 路径缺 key/config 当场以 stderr 报错并 exit 1；不打开 index、不调用 embedding、reranker 或 generator。
- [x] 成功向用户显示 provider/model、candidates、selected、attempts、latency；无 query 显示 `skipped reason=no_query`；失败显示 attempts 和“generation aborted”。
- [x] 状态由 retriever/CLI orchestration 输出到 stderr；generator 保持只负责生成，不接收 `rerank_success` 条件分支。
- [x] `search` 诊断结果可见 vector/rerank score 与 rank；普通 mock 不向 stdout 倾倒40条内部记录。

**完成定义：** CLI 测试覆盖 env/CLI precedence、缺 key、skipped/applied/failed 和 stdout/stderr 分离；secret 扫描无泄漏。

## 9. P6：运行日志、Schema 与离线可审计性

运行目录：

```text
outputs/rerank/
├── runs/<timestamp>-<command>-<run_id>.json
└── latest.json
```

说明索引写入 `docs/evaluation/RERANK-EVALUATION.md`，只记录 schema、命令、基线和实验 run 索引，不复制 JSON 数据。

- [x] 外部调用前创建 run 目录并写初始记录；初始化写失败时不调用 API。
- [x] JSON 每次通过同目录临时文件写入并原子替换；API 成功但最终评分记录写失败时整次命令失败，stderr 明示“reranker 已调用，generation 未开始”。
- [x] 每次 attempt 记录序号、时间、latency、HTTP status（如有）、三档错误类别和非敏感 provider request ID。
- [x] 每个候选记录本地 chunk_id、vector score/rank、rerank score/rank、最终位置、selected 和 drop reason；不把完整 chunk text 写入常规日志。
- [x] run 记录 provider/model、候选/最终预算、0.92阈值、source cap2、调用状态、usage/cost（如果可得）和总延迟。
- [x] 记录 query 原文的既定本地审计字段及 hash；缓存键使用 provider/model/query/候选内容 hash/rerank 配置，key 本身绝不参与明文记录。
- [x] eval exact-request cache 仅用于完全相同请求，离线接口支持 `no_cache` 强制绕过；普通 search/mock/coverage 第一版不做隐藏结果缓存。正式顺序 runner、断点恢复和 `from_cache` 审计留在 P9。
- [x] 若接入现有 generation trace，先升级 schema 并保留旧版本读取/明确拒绝边界；replay 只能使用保存结果，严禁重新调用 reranker。
- [x] 历史 outputs、trace、judge 和 challenge 产物不修改。

**完成定义：** 成功、三次外部失败、shutdown、2xx非法响应、API后日志失败都有完整且不泄密的终态；离线检查不访问网络。

## 10. P7：离线工程验收与独立复核

- [x] 新增 fake reranker 与 fake provider client，覆盖成功、tie、空候选、N<40、重复/缺失/越界 index、NaN/Infinity、三档错误和最大尝试数。
- [x] 覆盖 2xx→立即 schema failure、non-2xx→最多三次、第三次成功、三次失败、Ctrl+C 零重试。
- [x] 覆盖 topic `<200` 过滤、完全重复、cosine 0.92 边界、source cap2、扫描40补足10、规则后不足10。
- [x] 覆盖 search 不做长度过滤、no-topic 完全不构造 reranker、query 缺 key 在 `_open_store` 前失败。
- [x] 覆盖日志预写、原子替换、写失败阻止 generator、secret 不落盘、cache hit 和 `no_cache`；正式 resume/from-cache 运行审计留在 P9。
- [x] 保持现有全量 tests 通过；运行 Ruff 和 `git diff --check`。
- [x] 作者完成后由独立 reviewer 复查实现与 spec AC，另外运行网络封锁测试，确认默认测试和离线 replay 为零 API 调用。
- [x] 记录测试数、命令、结果和未执行的 live 范围；不把 fake-client 通过宣称为真实 provider 已验证。

建议验证命令：

```bash
.venv/bin/python -m pytest -q tests/test_reranker.py tests/test_rerank_selection.py
.venv/bin/python -m pytest -q tests/test_rerank_logging.py tests/test_rerank_cli.py
.venv/bin/python -m pytest -q tests/test_storage.py tests/test_eval_trace.py tests/test_eval_replay.py
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
git diff --check
```

命令中的新测试文件以实际实现名称为准；不得用不存在的命令冒充已执行证据。

## 10.1 P1～P7 实施与独立验收证据

本轮采用 fake adapter/transport 和空 key 覆盖完成离线实现，不读取本机 `.env` secret、不联网、不调用 OpenRouter。

| 证据 | 结果 |
| --- | --- |
| 目标测试 | `100 passed in 0.51s` |
| 全量回归 | `723 passed`，独立复跑约 `2.9s` |
| 静态与格式 | Ruff `All checks passed`；`git diff --check` 通过 |
| 网络/secret 边界 | `OVERFIT_RERANK_API_KEY=''`、`PYTHONDONTWRITEBYTECODE=1`；CLI help 不回显 secret；未执行真实 API |
| 独立复核 | code reviewer 与 verifier 均批准 P1～P7，明确要求停在 P8 前 |

独立复核首轮发现并在批准前修复：rerank 配置非 query 路径的懒校验、trace 使用 CLI override 后的真实 identity、调用前 attempt intent 持久化、非法 2xx 不得标 success、失败 attempts 计数、eval cache 的 endpoint/ordered candidate IDs 完整身份。修复后重新运行目标/全量测试、Ruff 与 diff check。

P8 后新增了真实 provider 可用性证据；P9 又完成了顺序双轨 runner、resume/from-cache 审计、47 题指标与完整费用报告。质量提升结论仅针对当前固定实验，不外推到未见题、Generator 或 judge。

## 11. P8：用户显式 Live Smoke Test（已完成）

此阶段已在用户本人明确授权后执行。

- [x] 用户通过本机 env 提供 key；运行记录只保存 provider/model 等非敏感身份，不打印 key。
- [x] 先用合成 query + 3 个合成 documents 验证鉴权和 schema，再执行一条 IFN580 question-only Top-40 请求。
- [x] 真实 Top-40 响应包含40个候选并选出10个，index/score、usage、latency、日志均可审计；本轮没有调用 Generator。
- [x] 本轮两次请求均一次成功；三档失败语义继续由已通过的离线 contract tests 保证，没有为 smoke 或单题增加特判。
- [x] smoke test 已通过，用户随后另行显式授权并完成 P9。

### 11.1 P8 真实调用证据

| 调用 | 结果 |
| --- | --- |
| Synthetic | query + 3 documents；`attempts=1`；Paris rerank rank 1、score `0.892326`，Berlin rank 2，banana rank 3 |
| Synthetic run | `outputs/rerank/runs/20261028T070403Z-live-smoke-synthetic-f78f1628-7633-475b-bc60-14a5c209297e.json` |
| IFN580 real Top-40 | week01 question-only，candidate budget/actual `40/40`，final/selected `10/10`；`attempts=1`；latency `1652.382 ms` |
| IFN580 top movement | `week1/introduction.pdf p13`：vector rank 2 → rerank rank 1；同文件 p21：vector rank 1 → rerank rank 2 |
| IFN580 usage/cost | `total_tokens=6172`；cost `$0.0003086` |
| IFN580 run | run ID `07752160-2933-4b31-87dc-3a9e119ba33b` |
| Policy/security | search selection policy，`applied=false`；日志没有 `api_key` 或 `authorization` 字段；未调用生成模型 |

**完成定义：** 两条用户触发、结构完整、可审计的真实 provider 记录均成功。这证明 OpenRouter 集成和 Top-40 wire contract 可用，不证明47题质量指标已提升。

## 12. P9：47题 Retrieval Eval 与报告

### 12.1 固定实验

- [x] 同一47题、同一索引和同一 dense Top-40 规则分别运行：`question-only` 与 `question+options`。
- [x] question-only 是严格诊断集；question+options 是最终产品决策优先参考，两组不混算。
- [x] 第一版 `concurrency=1` 顺序执行；支持缓存、resume 和 `--no-cache`，本次强制 `--no-cache`，未引入并发或 rate-limit 调参。
- [x] 每组报告 Hit@1/3/5/10、MRR@10、逐题 rank movement、成功/失败/缓存数、P50/P95 latency、调用次数、usage/可得费用。
- [x] dense baseline 与 rerank 使用同一候选集合；未临时改变 query 拼接、候选数、阈值或过滤规则。

现有基线：

| Query | Hit@1 | Hit@3 | Hit@5 | Hit@10 | MRR@10 |
| --- | ---: | ---: | ---: | ---: | ---: |
| question-only | 44.7% | 74.5% | 83.0% | 89.4% | 0.614 |
| question+options | 66.0% | 85.1% | 93.6% | 95.7% | 0.773286 |

期望目标而非硬完成门槛：question-only Hit@1 约 ≥52%、Hit@3 约 ≥80%、MRR@10 约 ≥0.67、Hit@10 不明显下降。无论提升、持平或下降，只要固定实验完整执行并如实报告，工程阶段都可以结束；不得为了过线反复调到47题过拟合。

### 12.1.1 P9 真实运行证据

- Run：`outputs/eval/rerank-20261028T072026Z-4a90e805-c4c9-40d5-94e1-3040e67ae23d/`；JSON 事实源为 `results.json`，摘要为 `report.md`。
- 快照：dataset SHA-256 `60d1455702a17df25119d4232b3ee2c95cda493cb255f4811d14de0107cb5a5f`；index SHA-256 `668fb3b946af45c0a73da1d398a245767d2f20a20d605d54358410c610b34e4a`，与正式 dense baseline 一致。
- 调用：`success/failure/cache=94/0/0`，`api_calls=attempts=94`，成功率 `1.0`，latency P50/P95 `945.7/1385.191 ms`，`total_tokens=683962`，cost `$0.0341981`。
- 上线前验收：全量 `731 passed`、Ruff 和 `git diff --check` 通过；独立 reviewer 复验 63 个目标测试，并确认 47×2 条 query 与正式 dense baseline 逐字节一致。

| 轨道 | Dense H@1/3/5/10 | Rerank H@1/3/5/10 | Δ H@1/3/5/10 | Dense → Rerank MRR@10（Δ） |
| --- | --- | --- | --- | --- |
| question-only | 0.446809 / 0.744681 / 0.829787 / 0.893617 | 0.595745 / 0.893617 / 0.936170 / 0.936170 | +0.148936 / +0.148936 / +0.106383 / +0.042553 | 0.614421 → 0.744681（+0.130260） |
| question+options | 0.659574 / 0.851064 / 0.936170 / 0.957447 | 0.744681 / 0.957447 / 0.978723 / 0.978723 | +0.085107 / +0.106383 / +0.042553 / +0.021276 | 0.773286 → 0.849291（+0.076005） |

两轨所有报告指标均为正向变化，question-only 也超过了全部首轮软目标，因此在这份固定 47 题实验上建议保留 reranker。评测产物不含 `api_key`、`authorization` 或 bearer 值，且未调用 Generator/judge；该结论不外推到未见题或生成质量。

### 12.2 结论分级

- 主要指标有可信提升：建议启用，并记录 latency/cost 代价。
- 指标互有升降：按 question+options 产品结果、逐题移动和错误类型决定是否保留。
- 明显下降：保留实现和日志证据，默认不宣称质量改善；不得静默换指标。
- API 失败未完成：报告技术失败，不把未评样本计成检索失败或成功。

## 13. Spec AC 映射

| Spec AC | 计划阶段 | 计划证据 |
| --- | --- | --- |
| RR-01 query触发/无enable/无query跳过 | P4、P5、P7 | 三条 query 命令集成测试；no-topic spy/fake 证明零构造、零调用 |
| RR-02 缺key fail-fast与CLI>env | P1、P5、P7 | `_open_store` 前失败；CLI/env precedence 与 secret 测试 |
| RR-03 Top40→最终预算及topic预过滤 | P2、P4、P7 | store candidate 测试、N≤40 payload、短文本过滤/不足不补 |
| RR-04 vector/rerank score/rank保真 | P2、P6 | 模型/序列化 roundtrip 和候选 JSON 审计 |
| RR-05 响应完整唯一有限+稳定tie | P1、P3、P7 | provider contract 的缺失/重复/越界/NaN/tie 测试 |
| RR-06 topic去重0.92/source cap2/顺序扫描 | P4、P7 | 选择边界、drop reason 和最终顺序测试 |
| RR-07 三档现象错误+3次/30s/1,2s | P1、P3、P7 | timeout/transport/non-2xx、shutdown、other 三组确定性测试 |
| RR-08 最小payload与secret | P0、P3、P5、P7 | 请求 snapshot 只含 `model/query/documents/top_n`，documents 只有文本；日志/异常/缓存 secret 扫描 |
| RR-09 原子JSON日志/fail-closed/stderr | P5、P6、P7 | 预写/原子替换故障注入、generator 未调用、stderr 断言 |
| RR-10 抽象层+首版单provider/不重建索引 | P0、P2、P3 | Protocol、唯一 adapter、原索引兼容测试 |
| RR-11 默认pytest无真实API、live仅用户显式 | P1、P7、P8 | socket 封锁全量测试、默认 skip、用户触发 live 记录 |
| RR-12 eval双轨/缓存/断点/顺序执行 | P6、P9 | **已完成**：离线 cache/no-cache/resume 验收，两轨各47题、concurrency=1 的正式报告 |
| RR-13 指标与软目标 | P9 | **已完成**：完整指标、成功率、latency/usage/cost 和软目标解读 |
| RR-14 回归/命令路径与退出行为 | P4～P9 | 全量 pytest/Ruff/diff、CLI exit、独立 reviewer 证据 |

## 14. 风险、回滚与非目标

### 风险与控制

- **Provider score 不可比：** 只比较同一请求内的排序；不设跨 provider 通用阈值，不拆批合并。
- **外部 API 不稳定：** 三次有界尝试、顺序 eval、缓存/resume；三次失败即中止，不 dense fallback。
- **2xx 但内容损坏：** 严格 schema 校验后立即失败，不让部分排序进入生成。
- **短标题占候选：** 仅 topic generation 路径预过滤；search 保留短精确答案能力。
- **多样性覆盖相关性：** 先 rerank 再按固定规则顺序扫描；日志保留每个 drop reason，后续以数据评估0.92/source cap2。
- **日志先于/晚于外部副作用：** 调用前预写、调用后原子更新；写失败宁可错杀并阻止生成。
- **CLI key 泄漏：** 接受显式 `--rerank-api-key` UX，但实现、异常和测试中统一脱敏；不将 argv/key 写入运行记录。
- **评测过拟合：** 两套 query 固定、目标非硬 gate、逐题变化和成本一起报告，不为达标临时调规则。

### 回滚

- reranker 是 query-time 层，不修改索引 schema或向量；代码回滚不需要重建 BGE-M3 索引。
- 如真实质量不佳，可回滚 query orchestration 与 CLI 接线，同时保留独立评测 JSON 作为实验证据。
- 不提供普通命令的隐藏 vector fallback；需要 baseline 时运行显式 eval/diagnostic 路径。
- 历史 outputs/trace 不原地迁移；新 schema 写新 run，回滚不重写旧记录。

### 非目标

- 第一版不更换 BGE-M3、不重建索引、不训练/微调 reranker。
- 不实现第二 provider adapter、自动 provider fallback、跨 provider ensemble 或 score calibration。
- 不实现并发 eval、通用 score threshold、动态 candidate count 或30/40/50参数搜索。
- 不把 reranker 放进 generator，也不把生成模型/judge 模型选择并入本计划。
- 不由 CI、fixture 或后台自动发起真实 API 请求，不在 CI 中保存或使用真实 key；P8/P9 的真实请求均由用户显式授权。

## 15. 最终完成定义

只有以下证据齐全才可宣称首版工程完成：

1. provider/model gate 有官方依据，唯一 adapter contract tests 和用户触发 live smoke 均通过；
2. query 默认 rerank、no-query 永不 rerank、缺 key 当场失败，均有 CLI/集成测试；
3. dense/rerank 分数与名次、候选筛选原因和完整 attempt 日志可审计；
4. 失败不进入 generator，日志失败也 fail-closed；默认 tests/replay 零网络；
5. 全量 pytest、Ruff、diff check 通过，并有独立 reviewer 证据；
6. 47题两套 query 结果、latency、usage/cost 和限制已如实报告；质量目标未达到不会阻止收尾，也不能被宣传为已改善。



