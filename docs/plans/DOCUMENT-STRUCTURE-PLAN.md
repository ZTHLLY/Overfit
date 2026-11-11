# 文档结构与弹性引用边界实施计划

> 文档版本：0.1 · 日期：2026-10-06 · 状态：**用户已授权核心实现；P0～P3 核心已实现并通过独立工程验收；P4 真实 PDF 留出/语义校准未完成**。文档复核与实现验收分别记录。
> 本轮按计划推进 P0～P4 核心实现和离线测试；不跑真实模型、不新增依赖、不自动重建现有索引、不 commit/push。
> 计划文档已通过独立复核；P0 实施契约见 spec 第 9 节，已另经独立评审。P5 仍是后续可选范围。

行为依据 [DOCUMENT-STRUCTURE-SPEC.md](/Users/silver/Documents/github/Overfit/docs/specs/DOCUMENT-STRUCTURE-SPEC.md)；judge 版本差异见 [judge spec](/Users/silver/Documents/github/Overfit/docs/specs/EVAL-JUDGE-SPEC.md)。检查项按本轮实际证据更新；未勾选不等于已完成。

## 1 交付分层与实施顺序

**MVP：先在已记录材料内应用弹性 ±1 定位，并保全已有来源。** 不以完美 PDF 识别、OCR、全量 reindex、新平台或补页服务为启动前提。

- 核心 P0～P4：冻结契约 → 来源元数据贯穿管道 → 已记录材料内的定位诊断 → 新协议记录/报告 → 独立验收。
- P1 可先用合成/内存材料完成 schema 和 roundtrip；真实 parser 仅在用户后续允许的开发样本上验证，不自动批量解析课程。
- 旧材料缺文档版本或真实总页数时保留 unknown；MVP 仍可记录模型原判与已知来源，不能假称已证明符合完整窗口政策。
- 可选 P5：**显式邻页补充引用复核**，仅在核心完成后另行授权；不是 MVP 必选依赖，未做就明确“不支持自动补页”。
- 原 generator ±2 修复、PR1 严格诊断、golden 严格页指标各自保留，不借新政策统一改口径。

依赖：P0 通过评审后才能编码；P1/P2 可按已冻结接口并行，P3 依赖其契约与行为；P4 工程 gate 通过后才宣布核心工程完成。P5 不影响核心验收结论，但不得被隐含宣传为已交付。

## 2 实施前代码事实与改动落点（历史基线）

| 落点 | 当前事实与未来责任 |
| --- | --- |
| [models.py](/Users/silver/Documents/github/Overfit/src/overfit/models.py) | Page/ParsedDocument/Chunk 未贯穿文档版本、可信总页数和多 provenance；补模型本身不足以保全端到端信息 |
| [parser.py](/Users/silver/Documents/github/Overfit/src/overfit/ingestion/parser.py) | Docling `_docling_page_of` 取首个可用页；须保留全部已知来源，页数 fallback 最高已见页不能冒称真实总页数 |
| [chunker.py](/Users/silver/Documents/github/Overfit/src/overfit/ingestion/chunker.py) | 已支持连续跨页 page/page_end；保留此能力，新增来源映射不能强制一页一块 |
| [pipeline.py](/Users/silver/Documents/github/Overfit/src/overfit/ingestion/pipeline.py) | 当前已有 parser backend 和 chunk 参数的 index profile 检查，不匹配会拒绝打开；通过该检查后按内容 hash 跳过文件，仍需纳入提取器版本和处理策略身份的失效判断 |
| [store.py](/Users/silver/Documents/github/Overfit/src/overfit/storage/store.py) | SCHEMA_VERSION='1'，chunks 仅已有扁平来源列，IndexProfile 仅记 parser 名；需显式 schema/兼容策略与读写 roundtrip |
| [selection.py](/Users/silver/Documents/github/Overfit/src/overfit/query/selection.py)、[retriever.py](/Users/silver/Documents/github/Overfit/src/overfit/query/retriever.py) | 选材、向量包装与检索返回不能丢弃新增元数据，不改变原选材算法来迎合某道题 |
| [generator.py](/Users/silver/Documents/github/Overfit/src/overfit/query/generator.py) | 保全 pre/post_policy 引用关联，现有 ±2 修复仍独立；不在生成器里新增语义特判 |
| [contracts.py](/Users/silver/Documents/github/Overfit/src/overfit/evaluation/contracts.py)、[trace.py](/Users/silver/Documents/github/Overfit/src/overfit/evaluation/trace.py)、[live.py](/Users/silver/Documents/github/Overfit/src/overfit/evaluation/live.py) | strict 记录契约与 `asdict(chunk)` 快照须一起考虑，不能新字段自动流入旧协议 |
| [replay.py](/Users/silver/Documents/github/Overfit/src/overfit/evaluation/replay.py)、[citations.py](/Users/silver/Documents/github/Overfit/src/overfit/evaluation/citations.py)、[report.py](/Users/silver/Documents/github/Overfit/src/overfit/evaluation/report.py) | PR1 严格事实不改义，新弹性诊断单独标识；回放不读取新材料 |
| [judge.py](/Users/silver/Documents/github/Overfit/src/overfit/evaluation/judge.py)、[runner.py](/Users/silver/Documents/github/Overfit/src/overfit/evaluation/runner.py)、[metrics.py](/Users/silver/Documents/github/Overfit/src/overfit/evaluation/metrics.py) | 新政策/rubric/协议身份，保持模型、技术、人工三层；旧重建严格核验 schema3/protocol/checker hash/tasks 与 source |
| [challenge.py](/Users/silver/Documents/github/Overfit/src/overfit/evaluation/challenge.py)、[cli.py](/Users/silver/Documents/github/Overfit/src/overfit/cli.py) | challenge `_Chunk` 是 extra=forbid 的旧字段集合；新增来源须版本化接入，CLI 仅在必要时后续设计，不预告不存在的选项 |

## 3 P0：冻结数据、政策与兼容契约（所有编码的前置 gate）

- [x] 保存基线：代码状态、既有索引/依赖/golden、真实 trace/judge/challenge 和相关 schema/hash；不回滚用户改动。
- [x] 冻结文档版本标识与真实物理总页数的来源和可信度：文件内容身份与提取器/策略身份分开；未知采用明确空值/状态，不从最大已见页猜总页数。
- [x] 冻结最小可选来源结构：一个逻辑 chunk 可有多个 page/span/bbox/结构关联，已知事实与推断信息区分；不要求每条都有精确字符范围。
- [x] 冻结元数据在 models → parser/cleaning/chunker → storage → selection/retrieval → generation snapshot → judge/challenge 的传播和序列化契约。
- [x] 冻结旧 schema 的读取边界、新 schema 身份及未知字段策略：不把新增字段硬塞旧 strict 协议，不静默删除再假装保全。
- [x] 冻结锚点方案：MVP 建议以该次审查的 `post_policy` 最终引用为锚点，明确 `anchor_origin=post_policy` 的概念；最终字段名在编码前确认。
- [x] pre_policy 原生成引用和 ±2 修复记录独立保留；不得声称“相对生成原引用总共仍只偏 ±1”，不得串联修复后窗口继续扩窗。
- [x] 冻结一次性 `W=[max(1,a-1),min(P,a+1)]`、错文件/版本/非法页/未知总页数/巨大 chunk 的诊断分类及人工路由，不制定词汇级语义规则。
- [x] 冻结新 policy/prompt/trace/judge/challenge schema 身份与旧记录策略；支持旧实现或明确拒绝，不暗中用新规则解释旧记录。
- [x] 冻结实验索引路径/生命周期、开发样本与留出验收样本清单、测试预算；本计划不发明已存在的 CLI 或环境变量。

**完成定义：** 作者和 reviewer 能依同一契约判断每条边界；pending 选择已写明决策，不以代码偶然行为替代决定。
**评审 gate：** 独立 reviewer 确认不扩大已批准政策；仍有 schema/锚点/兼容冲突则只补文档，不推进实现。

## 4 P1：最小来源元数据保全与管道贯通

依赖 P0。先用合成对象与 mock 提取器完成工程链路，再按授权做有限真实样本检查。

- [x] parser 在原本显式解析文件的路径取得文档版本与可信真实页数；不是让 judge/replay 回头读 PDF。无法可信取得时为 unknown。
- [x] 保留 Docling item 的所有已知 provenance，不只挑第一页；清理重建 Page 时也保留来源与结构关联。
- [x] 跨页 item 仍可一个 chunk；页范围只是粗粒度摘要，多来源附属信息不能被 page/page_end 压掉。
- [x] 保全提取器已经给出的标题/正文、表格、多页来源与图文关联；picture children 和显式 caption refs 保留 context，长切片也向模型传结构标签。未识别的跨页续表、行列/空间关系不靠规则补写，不把元数据保全宣称为完整语义恢复。
- [x] 文本提取器无 bbox 或图片不可恢复时诚实标明限制；不把完美 OCR、公式恢复或所有图表理解设为 MVP 前提。
- [x] chunker 保留连续跨页和原文范围映射；连接符、trim/清理和 overlap 后的 span 必须可解释，不伪造精确映射。
- [x] store 新 schema 显式读写这些字段；模型→落库→检索→选材→生成快照→重新载入 roundtrip 不丢信息。
- [x] cache key/失效包含内容身份、parser/backend 和版本、清理/chunking/provenance 策略身份及相关参数；不能仅 source hash 相同就跳过重处理。
- [x] 新索引实验使用隔离路径，老索引保留；迁移/重建另行确认，不自动全量覆盖、不顺带请求 embedding 服务。
- [x] 旧记录缺来源只读为 unknown，不偷偷追随路径补字段；只从已知内容作诊断，未取得可信总页数不宣称窗口已完整验证。

**测试落点：** 扩展 [test_parser.py](/Users/silver/Documents/github/Overfit/tests/test_parser.py)；计划新增 `/Users/silver/Documents/github/Overfit/tests/test_chunker.py`、`/Users/silver/Documents/github/Overfit/tests/test_document_metadata.py`，覆盖存取和全链路 roundtrip。
**完成定义：** 合成/假提取器的多来源与可信度均不丢，旧缺失数据不被伪造；缓存版本变化和隔离索引策略可复验。
**评审 gate：** 独立检查多 provenance、picture/context、源身份及缓存失效；不把语义理解交给规则代码验收。

## 5 P2：已记录材料内的弹性定位 MVP

依赖 P0 的已冻结接口；可与 P1 的存储工作分开开发，但集成前必须合并验证。

- [x] 在新政策身份下实现纯函数定位诊断，只读取输入快照；不检索、不打开 PDF、不加载模型。
- [x] 围绕冻结锚点计算一次 ±1，真实页数裁剪；首/尾页、±2、跨文件/版本、非法锚点与总页数未知分别记录。
- [x] 大 chunk 仅范围相交不能被标为整个正文在窗口内；已知 provenance 指向具体子片段，未知粒度保留限制，不强制拆正文求过关。
- [x] 同文档已知窗口内的偏差、仅跨页本身只作说明，不单独触发人工；unknown/partial/矛盾和实质来源问题仍按既有职责待审。
- [x] 保留模型原始 verdict；程序不根据窗口命中赋 supported，不用课程关键词决定论断正误。
- [x] 新 rubric 解释定位容差与语义责任，强调否定、前提、表格行列归属和结论支持不得被页码容差豁免。
- [x] 使用原生成材料保持 answerability 无答案隔离，material_support 不增加原来没给过的正文；本阶段不提供补充上下文。
- [x] PR1 严格 diagnostics、generator ±2 修复及 golden 严格指标独立回归，不把它们改成新定位口径。

**测试落点：** 扩展 [test_eval_citations.py](/Users/silver/Documents/github/Overfit/tests/test_eval_citations.py)、[test_judge.py](/Users/silver/Documents/github/Overfit/tests/test_judge.py)、[test_judge_metrics.py](/Users/silver/Documents/github/Overfit/tests/test_judge_metrics.py)；新政策样本与旧协议样本清楚区分。
**完成定义：** 窗口正负边界和人工路由满足 spec，模型原判/技术完成/人工状态保持独立；缺来源数据不靠隐式补页“修好”。
**评审 gate：** reviewer 检查没有路径/页号/关键词特判，也没有“命中窗口=语义通过”的新规则。

## 6 P3：版本化记录、报告、回放与 challenge 接入

依赖 P1/P2 的契约与核心行为。

- [x] 更新受影响的 strict schemas 和安全序列化，冻结消息/实际材料/来源元数据/锚点/政策身份写入新记录，不只修改 Chunk dataclass。
- [x] 检查 `asdict(chunk)` 新字段与 trace/challenge 白名单的交互；旧协议不能无声明地接收或剥离新字段。
- [x] 记录新政策、prompt、实现、实际输入与预算身份；安全配置不泄密，记录失败停止后续调用的边界不削弱。
- [x] 报告并列原模型语义结论、严格引用诊断、新弹性定位诊断、技术/人工状态；外部锚点或短页范围与内部多 provenance 均可审计。
- [x] 明确报告“原材料评审”“未执行补充引用复核”，不让核心交付被读成已有补页能力。
- [x] 新协议只写新目录，历史 trace、judge-v3 和 challenge 产物与报告不改；各自协议独立，旧记录支持或明确拒绝必须符合 P0 决策。
- [x] replay 完全离线，仅读保存输入和当前支持的对应政策；原 checker hash/tasks/schema 校验不得为兼容新字段而直接移除。
- [x] challenge 新来源格式另行版本化，input/annotations 隔离、配对 before/after 与原 bytes 快照保持；不偷偷迁移旧 suite。
- [x] CLI 仅在 P0 已决定需要显式新入口/开关时修改，并先文档化；默认不触发解析、索引更新或模型服务。

**测试落点：** [test_eval_trace.py](/Users/silver/Documents/github/Overfit/tests/test_eval_trace.py)、[test_eval_replay.py](/Users/silver/Documents/github/Overfit/tests/test_eval_replay.py)、[test_generator_trace.py](/Users/silver/Documents/github/Overfit/tests/test_generator_trace.py)、[test_judge_runner.py](/Users/silver/Documents/github/Overfit/tests/test_judge_runner.py)、[test_judge_replay.py](/Users/silver/Documents/github/Overfit/tests/test_judge_replay.py)、[test_judge_challenge.py](/Users/silver/Documents/github/Overfit/tests/test_judge_challenge.py)；必要时扩展对应 CLI tests。
**完成定义：** 新 metadata 全链路 roundtrip、原始响应可重验、历史产物不变，旧协议兼容或拒绝行为明确且有测试。
**评审 gate：** 独立核查协议身份、输入完整性、写失败/中断和回放边界，不以删校验换兼容。

## 7 P4：独立验收、样本隔离与核心交付

开发样本与验收样本在 P0 分开，留出文档不用于反复调整规则；可以来自已有授权材料和合成内容，不要求重标全套 golden。

- [ ] 开发样本覆盖连续正文、幻灯片、跨页表格、图+图注/轴文字、空白边界、提取信息不足；不只选择原出问题 PDF。
- [ ] 留出验收文档异质且未参与调参，至少覆盖不同结构/版式与来源粒度；若看过其失败后修改实现，须重新标为开发样本并补独立留出。
- [x] 合成确定性 tests 单独证明 ±1/±2、固定锚点、首尾裁剪、错文件、未知身份/总页数、巨大 chunk、不串联 ±2 修复等工程边界。
- [x] 已有来源/结构的合成保全与图文输入隔离通过独立工程检查；不将字符串存在或 metadata 往返视为语义正确，真实材料语义校准仍未完成。
- [ ] 语义观察单独记录模型/人工对正负边界的解释、unknown/partial/分歧，不用代码把预期标签一致当最终质量批准。
- [x] 初期只运行假客户端和禁网络测试；真实解析、模型观察与预算需后续单独授权，不能因写好 plan 就自动调用。
- [x] 跑全量 pytest、Ruff、diff check，保全历史文件 hashes；独立 reviewer 再跑关键边界并检查未触及 golden/原索引/配置。
- [x] 依据实际证据回填计划与文档；报告核心已做哪些、未做哪些、旧协议如何处理，明确 P5 补页仍未实现。

**完成定义：** P0～P3 所有 gate 通过、全量工程回归与独立复核无阻塞，限制可见；未经授权的在线语义观察明确未进行。
**评审 gate：** 作者与 reviewer 分开；单 PDF 改善或测试全绿不能证明通用语义可靠，不宣称已完成生产校准。

## 8 P5：可选邻页补充引用复核（后续单独批准）

依赖核心 P4。优先设计**独立 citation-only 请求**，避免补充正文污染原 answerability/material_support；不是当前双阶段请求的隐式扩容。

- [ ] 另行冻结显式触发方式、资料来源、许可与预算；不在 prepare/replay 或普通评审时自动读 PDF、重解析、检索或联网。
- [ ] 在新记录分别保存 original_generation_context 与 supplemental_review_context，保留文档版本/真实页数/锚点/固定窗口/选择理由/顺序/正文 hash/实际发送内容。
- [ ] 补充选择只围绕原冻结窗口，不按新发现证据递归扩展；必要窗口外结构上下文单独标范围和用途，不冒充窗口内证据。
- [ ] 保存额外请求、输出 token/输入预算、usage、调用/重试与是否受限；不能截去否定/表头后宣称关系完整。
- [ ] 请求身份与任何未来缓存键包含政策/prompt/文档版本/原材料和补充材料摘要/选择与预算；不能仅凭题目或源文件名复用。
- [ ] 原输入 unsupported/not_answerable 不因后补页而转正；独立展示“原输入不足；补充引用材料有/无依据”，保留不确定性。
- [ ] 补充结果只写新产物，离线 replay 使用保存内容而不重新补页；历史 v3 记录不得覆盖或冒称曾接受此请求。
- [ ] 新增未来 `/Users/silver/Documents/github/Overfit/tests/test_supplemental_citation_review.py`，验证输入污染、预算/记录故障、版本缓存失效和离线回放边界。

**完成定义：** 上述能力有独立测试与真实记录契约，是否线上验证另列；未完成之前核心报告始终声明未补页。
**评审 gate：** 独立 reviewer 确认补充材料不能“洗白”原输入评审，不借可选范围改动已冻结核心政策。

## 9 本轮实施与验证证据

用户已授权核心实现。P0 冻结契约独立通过后分工实现；作者与 reviewer 分开。工程核心不包含真实 PDF 留出质量证明、在线语义校准或 P5 自动补页。

| 证据 | 本轮结果 |
| --- | --- |
| 基线 | `.venv/bin/python -m pytest -q`：525 passed；Ruff 通过 |
| 最终工程回归 | 独立 reviewer：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q` **664 passed in 2.01s**；`.venv/bin/python -m ruff check src tests` 和 `git diff --check` 均通过；无剩余工程阻断 |
| 历史保全 | 基线覆盖 outputs/index/eval 及 pyproject.toml、uv.lock、.env 共 220 个既有文件；最终核对 **220/220 未变** |
| 来源契约 | provenance schema1；新 index schema2；模型构造、存储、trace、challenge 边界重验；源文件解析前后摘要一致，pipeline 检查其与缓存 hash 一致 |
| 回放/协议 | 新 trace2/materials2、judge-v4/schema4/citation-window-v1、challenge-v2/schema2；旧 trace1 可读，旧 judge-v3 明确拒绝新 checker 回放；缺版本不能默认为 v2 |
| Roundtrip | `tests/test_eval_provenance.py`：临时 SQLite 假向量 → 选材 → trace → 离线 replay → judge tasks/request → challenge；审计原文 sentinel 不进入模型输入 |
| 操作边界 | 未启动 Ollama/调用真实 API、未解析外置盘 PDF、未安装依赖、未迁移/重建原索引、未 commit/push |

### 独立复核发现与修复

1. `docling+formula` 也纳入 Docling/Torch/Transformers 依赖版本失效，不只处理 `docling`。
2. SQLite `mode=ro` 仍可能创建 WAL/SHM：旧索引改为核验过的临时不可变快照，原目录不写 sidecar；非空 WAL 明确拒绝，不静默丢失已提交事务。此打开限制也适用于 schema2。
3. 混合旧/新 Page 时保留已知 blocks，未知文字留下映射空缺；整体身份无法证明时为 unknown，不丢掉已知页信息。
4. 长图文字切片没有外围 marker 时仍向 generator/judge 传当前承载范围的结构标签；保留显式 caption refs，不推断图内空间关系。
5. 解析过程源文件变化时 fail closed，不能把旧文本绑定新 SHA；pipeline hash 与解析 hash 不同也不写缓存或请求 embedding。
6. trace reader 要求原 JSON 显式声明版本，缺失/重复/非法版本不能借模型默认值升级为 v2。

这些暴露问题后参与修复的用例归为开发/回归，不冒称未参与调参的留出。独立检查位于 `tests/test_document_structure_independent.py`：**24 项通过**，其中 20 项为开发/修复回归；修后另增 4 项组合首次通过（单页边界、末页跨页、非相邻页 coarse 表格原坐标不同域，以及缺原 PDF 的 store→trace2→judge4 prepare→离线重构）。这些仅是独立工程验证，真实异质 PDF 留出仍待授权执行。

### 样本与剩余工作

- 开发/回归：`test_parser.py`、`test_chunker.py`、`test_document_metadata.py` 的合成正文、清洗、表格、图片子项/图注、多来源及未知字段；`test_elastic_citations.py` 的固定窗口与人工路由边界；临时 schema1/schema2 索引与假客户端记录。
- 工程可证明：已知元数据不丢、实际正文/审计原文隔离（补充上下文未实现）、固定窗口不滚动、原模型 verdict 不被程序改写、版本与历史保全。不将字符串存在检查称为语义理解。
- **未完成：**真实异质 PDF 留出、真实模型/人工语义校准、P5 citation-only 邻页补充；不需为这些重标整套 golden。
- 现有 CLI 用法保持。已有 trace 可新建 v4 审查；无来源 metadata 时仍是 `unverified`，不回读 PDF 修补。旧 schema1 索引可读不可写，后续重处理必须使用单独隔离索引路径；本次没有执行。
