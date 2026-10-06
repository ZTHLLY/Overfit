# Eval 独立 Judge Spec

> 版本：0.3 · 日期：2026-10-06 · 状态：judge-v3 职责修订已实现并通过独立离线验收（439 tests）；未执行新模型调用或在线 v3 pilot，真实 judge 校准未完成。
> 范围：PR 2 独立语义审查。用户已授权写代码；本轮不调用真实模型，不执行 commit/push。

总体目标见 [EVALUATION.md](/Users/silver/Documents/github/Overfit/docs/EVALUATION.md)，实施与证据见 [EVAL-JUDGE-PLAN.md](/Users/silver/Documents/github/Overfit/docs/plans/EVAL-JUDGE-PLAN.md)。PR 1 的 trace 协议不变；本规范描述新增的旁路审查，不重新生成题目、不修改 golden 或索引。

## 1 目标与边界

现有引用检查回答“文件和页码是否属于生成时提供的材料”。本批新增的 judge 回答“这些材料能否支持题干、答案及其引用，题干本身能否作答”。

审查对象是完整 trace 中的**最终保留题**，材料只能来自该 trace 的实际输入。先执行离线完整性校验，再从 `citation_processed.post_policy` 读取题目，保留 PR 1 的 `item_id`、原始和最终引用关联。不能从题卷 Markdown 猜题目或重新检索补材料。

交付内容：

- 显式独立 judge 配置；默认准备模式不联网、不读取生成模型配置代用。
- 无答案的可作答性审查，与允许读取答案的材料／引用支持审查分开调用。
- 严格结构化输出、可选摘录定位诊断、来源疑点转人工、错误与预算处理。
- 独立审查目录、完整可见请求与响应、逐题判定和易读报告。
- 保存判定后的纯离线复算；不把旧汇总结果当事实源。
- 假客户端与合成材料的离线测试，保持 PR 1 回归通过。

本次修订不包含：重新运行真实 judge pilot、人工校准、原 PDF 图像核验、自动改题、重写生成 prompt、被丢弃题的语义审查、跨运行缓存自动复用、断点续跑、并行请求、大型评测平台、MCQ／golden 检索、发布门槛。

被丢弃和修复前题目仍能通过 PR 1 记录审计，但本批只给最终保留版本语义结果；原始版本不能借最终结果冒称已评。没有完整 trace 的历史题卷不属于本入口支持范围。

## 2 执行方式与配置隔离

CLI 契约冻结如下：

```bash
# 默认只准备任务与报告，不发 judge 请求
uv run overfit eval judge /absolute/path/to/run

# 独立配置和预算齐备后，显式执行
uv run overfit eval judge /absolute/path/to/run --execute --max-calls 6

# 可选只审前 N 题；其余保留题继续计入分母且标为未评
uv run overfit eval judge /absolute/path/to/run --execute --max-calls 6 --max-items 3

# 纯离线重算既有 judge 记录
uv run overfit eval judge-replay /absolute/path/to/judgment
```

离线复算通过独立 `eval judge-replay JUDGE_DIR` 入口执行，不能与 PR1 的 `eval replay` 混用。CLI 必须显示报告绝对路径、技术状态、已选任务完成数、全量保留题覆盖、模型结论与待人工任务数。不得将“请求返回”“结构完整评审”“模型全正向”混成一个自动通过数。

执行状态只表达**本次选中任务的技术完成情况**：选中任务全部取得完整可解析评审即 `completed`，即使有模型负判、unknown、来源疑点或待人工；其他未选中题仍保留 `not_evaluated` 及全量覆盖分母，不使本次运行 incomplete。选中任务因预算、错误、输入限制等未完成才为 `incomplete`；中断为 `interrupted`。prepare 为 `prepared`。退出码 completed/prepared=0、incomplete=1、interrupted=130；记录损坏拒绝返回非零。`--max-items 1` 审完两阶段应显示“已选 2/2，全量 2/6”，不是零完成。技术完成不等于题目获批。
### 2.1 无隐式付费服务

Judge 使用单独的 `JUDGE_` 配置，不继承生成器的模型、端点或凭证。缺配置时默认准备流程可以成功，所有项目为 `not_evaluated`；`--execute` 配置无效时在任何服务调用前停止并明确缺项，不替用户选择服务。

`JUDGE_MODEL`、`JUDGE_BASE_URL`、`JUDGE_API_KEY` 为独立必填项，只有 execute 加载。`--max-calls N` 在 execute 必须显式提供正整数；`--max-items N` 可选，按最终题顺序选前 N 题，不删除剩余题的未评记录。

`JudgeSettings` 默认超时 60 秒、每任务最大尝试 2 次、输入字符上限 80,000、单次输出上限 2,000 tokens、temperature 0、response_format `json_object`；不自动切换 response format。本项目用户已将本地 `.env` 的输出上限调为 4096；该显式运行配置高于代码默认，本修订不改动它或泄露密钥。接口包含 `build_request`、`make_client`、`invoke`、`validate_review`、`protocol_digest`、`safe_identity`，runner 负责全局调用数而不是让这些函数各自无限重试。没有已确认的服务价格不估算虚构费用，成本字段为未知。调用次数和 token 参数是可执行资源边界，**不是保证精确金额的预算器**。

准备和离线复算不得初始化 OpenAI 客户端、读取生成 `Settings`、打开数据库、探测 embedding 维度或发网络请求。线上配置可以在 `--execute` 路径读取，但只将白名单安全字段写入审查 manifest；剥离 URL 的 userinfo、query、fragment，不保存 API key、headers、原始异常 repr、环境文件或客户端对象。

### 2.2 固定预算与重试

每道最终题按固定顺序执行两个任务：`answerability` 和 `support`。无重试时 N 道题最多产生 2N 个 judge 客户端调用。两任务使用独立消息列表，不把任一任务的响应传给另一任务。

- 全局最大调用数包含失败、格式修复和重试，不为每题重置。
- SDK `max_retries=0`，避免 SDK 在不可见处突破 runner 的调用上限。
- 仅可重试传输错误或无效结构按固定上限重试；证据错误不重试；有效的负判、部分支持、unknown 都直接接受，不重试追分。
- 每次重试仍消耗全局调用预算；耗尽后剩余维度明确为 `not_evaluated`／预算耗尽，不能跳过题目缩小分母。
- 输入超限时不静默截取材料或答案；该任务记未完成及原因，不给正向结论。服务明确输出截断或未返回完整响应时不得把部分 JSON 当完成判定。
- 输入字符／字节边界不是精确 tokenizer 限制；服务上下文不足仍属于可记录的评测错误，不冒充材料不支持。
- 请求记录写入失败时，立即停止，不再消耗后续调用预算。

## 3 三个审查维度

| 维度 | Judge 允许看见 | 判定枚举 |
| --- | --- | --- |
| `answerability` | 固定 rubric、仅题干与完整实际材料（含 chunk 来源标签，不含待评题的 citation 或 topic） | `answerable`、`not_answerable`、`unknown` |
| `material_support` | 固定 rubric、题干、生成答案、最终引用、完整实际材料 | `supported`、`partially_supported`、`unsupported`、`unknown` |
| `citation_support` | 与 material support 同一次请求；判断范围明确为最终引用的具体页 | `supported`、`partially_supported`、`unsupported`、`unknown` |

`answerability` 请求不得包含来自生成答案字段的内容、生成模型自评、支持判定、参考答案提示、之前审查响应；不能只在 prompt 里说“忽略答案”却把答案字段一起发送。隔离按字段来源验证，不做答案字面 substring 禁令；材料本身自然可能含有回答题目所需的文字。它不是自动答题基准，不要求输出完整新答案，只需判定、证据与简短理由。

### 3.1 材料支持

判定题干中的知识性前提、答案的关键论断、必要推导是否得到本次实际材料支持。允许改写、比较、由题目明确给定的假想条件及材料支撑的推导，不要求题目原句出现在课件里。

全部必要论断受支持才能 `supported`；claims 由 judge 自行枚举，代码对“列出的 claims 全 supported”的一致性核验不证明它确已穷尽所有必要论断，仍需校准。部分关键论断有依据但仍有缺口为 `partially_supported`；`unsupported` 若同时列出正向 supported／partially_supported claim，则记录一致性冲突，而不是 JSON/schema 错误，也不由代码替它改成其他语义结论。存在支持部分与缺口的混合情况，rubric 指导模型使用 `partially_supported`，不把评测错误包装成 unsupported。关键结论无依据或与材料冲突可由 judge 判不支持；文本缺失、公式／图片提取不明等无法可靠判断的情况为 `unknown`。不能用常识或外部知识补齐 trace 未提供的依据。

题目明确给出的假设、情景设定、变量取值、作答指令（例如“解释原因”“比较两种方法”）不是必须在材料中逐字证明的知识断言，不应单独列成缺少证据的 claim。模型仍需检查材料能否支撑在这些假设下的必要推导；代码不通过关键词、字符串或人工枚举课程知识规则来替代语义判断。

### 3.2 可作答性

判断仅凭题干和提供材料是否具备所需条件，有无缺图、歧义、遗漏假设或无法完成的必要推导。无须精确唯一措辞；但材料中有相关关键词不能代替条件充分。

`not_answerable` 是内容判断；服务超时／无效 JSON 为执行或结构错误，已解析但无法定位摘录仅附定位注释，模型 unknown 另进入人工队列；这些状态均不能混为不可作答。

### 3.3 引用支持与跨页限制

其他页存在充分证据，不能掩盖最终引用页不支持结论。引用支持与材料支持分别输出；PR 1 的 source/page 合法性仍由代码复算。

代码仅核对证据所属 chunk 的来源与页级范围，不判断其是否逻辑蕴含答案。单页 chunk 与最终引用 source/page 一致，可以记录来源核对通过；错来源、错页或未知 chunk 时记录实质疑点并进入人工复核。仅当 citation_support 为 supported/partially_supported、主张具体页支持而证据只有跨页范围时，新增页级不确定的人工理由；清晰负判不因跨页本身转人工，unknown 按其自身不确定性转人工。跨页 chunk 可用于整体材料支持或可作答性。正向具体页结论即使还有其他单页证据，跨页疑点仍让人工检查，但不能覆盖 judge 的 citation verdict，也不能从技术完成数中扣除。

## 4 模型、程序与人工的职责

### 4.1 三层独立结果

**Judge 判断语义，程序核验技术和来源并分发待复核任务，人工解决疑点；代码不是第二个语义裁判。**

`answerability` 输出包含判定、reason 与 evidence；`support` 输出包含 `material_support`、`citation_support`、`reason`、`claims: [{text, verdict, evidence}]` 与 `citation_evidence`。Evidence 为 `{chunk_id, quote?}`：`chunk_id` 必需，`quote` 为可选解释性摘录（省略、null、空串或纯空白允许，解释为未提供；非字符串非 null 仍是 schema 错误），不要求模型逐字抄写整段。原文 offset 只由可选定位器计算。模型应给清楚、简短依据，对材料缺失、模糊或部分支持如实说明，不能为追求正向结论补外部知识。

逐任务独立保存并展示：

1. **模型结论**：完整 `model_review` 的原始值，包括 verdict、reason、claims 和可选 quote。程序不覆盖、不因诊断丢弃，也不将正判替换成代码生成的 unknown。
2. **技术状态**：完整、结构合法的响应为 `evaluated`；传输／截断／schema 错误为 `error`；未调用或未选中为 `not_evaluated`。可疑或负向结论也计入完整评审覆盖。`raw_response` 保存捕获的可见 content；部分 JSON 不组成完整模型评审。
3. **人工复核状态**：独立 `human_review`，包含状态与原因；`pending` 表示需要人工，`not_required` 在 UI 显示“未触发人工复核”，表示本规则未发现必须介入的疑点，`not_evaluated` 表示尚无完整评审。`not_required` 不等于人工批准或真实正确。本轮不实现人工裁决录入，不伪造 reviewed/approved。

精确 phase 字段为 `status`、`technical_status: completed|error|not_evaluated`、`reason`、`raw_response`、`model_review`、`verification: {structure,evidence,consistency,diagnostics,evidence_records}`、`human_review: {status,reasons:[{code,path}]}`。结构验证后 `model_review` 深拷贝原 decoded 对象，保留原 shape，不用 schema 默认值补写模型没有提交的字段。输出可选 `human_review_required: bool`（缺省按 false 解释）；模型认为有疑点时设 true 并在 reason 解释。原始对象未提供此字段则不补入 model_review。删除原派生 `review` 字段。

`verification` 仅记录结构、来源、定位和一致性诊断，不能通过 accepted/rejected 或“可采纳性”再次裁决语义。证据／一致性 verification diagnostics 有 code/path/severity：定位注释为 info，实质复核原因为 review。schema/transport/truncated 等技术 diagnostics 只要求 code/path；human_review.reasons 也为 code/path，不要求 severity。模型不确定／模型主动要求复核等人工原因只进 human_review，无须重复作为技术诊断。verification.evidence/consistency 使用 valid|diagnostics|not_checked，不使用 needs_review。原 `needs_review` 不再是阶段技术状态，原 `admissibility` 与 `confirmed_pass` 自动批准语义废弃。模型事实源始终为保存的 model_review，不提供第二份可被误当最终裁决的 review。

### 4.2 何时转人工

以下可解析评审保留原判、计入技术完成，同时进入 `human_review.pending`：

- 模型输出 unknown 或 partially_supported（包括分项 claim），即存在不确定或边界；或显式 human_review_required=true。
- 模型总判和其列出的分项矛盾；程序只指出矛盾，不替它选择一个正确判定。
- 正向声明缺少必要来源证据、正向材料结论缺 claims，或证据 chunk 不存在。
- 引用来源／具体页不一致；或正向 citation_support（supported/partially_supported）用跨页证据主张具体页支持，无法确定具体页。清晰负判不因跨页本身新增人工理由。

清晰的 unsupported/not_answerable **本身**不是必须人工介入的理由；仍如实展示负判，若同时有上述疑点则转人工。明确结果也可后续抽样校准，但本批不实现随机抽检调度。

所有这些情况不按 schema_error 重试；仅沿用固定预算内的传输／结构错误重试。程序不根据课程关键词、文本相似度或模型正判比例决定语义标签，不自动删除不利 claim 来凑通过。列表完整性依旧是模型能力和后续校准问题，代码不能证明 claims 穷尽了全部必要论断。

### 4.3 摘录定位只是辅助诊断

给出 quote 时，先 exact 子串匹配，再仅容忍 Unicode 空白折叠；匹配后保留映射回原文的 `[start,end)`、原文摘录、模型 quote、匹配方式与 chunk 来源。重复位置不擅自挑最有利片段，非空白改写不冒充精确命中。

但是 `quote_not_found`、`ambiguous_quote`、无 quote 或空白 quote **仅为 info**：既不使完整评审归零，也不因这一个原因转人工。省略号／改写不能精确定位，不等于不存在语义依据；允许附注“未逐字核验”，不能说引用已逐字验证。此规则不承诺判断摘录是否编造；若需要这样的语义判断，由 judge 或人工进行，而不是字符串规则替代。

每条 evidence record 的 `source_status` 与 `quote_status` 独立，即使 quote 找不到也继续核对来源页。记录保留 path、chunk_id、model_quote、status、match_method、start/end、原文 quote、source/page/page_end。未知 offset 为 null，不伪造跨度。chunk 未知属于来源疑点而非普通定位注释。定位成功也仅说明文字存在，不证明逻辑支持。

证据、题目和材料都是数据，不得作为指令执行；报告安全呈现不可信 HTML/Markdown。只保存可见 content、usage、时延、安全调用身份，不请求或保存内部 reasoning、密钥或原始异常 repr。

## 5 持久化与离线复算

每次 prepare 或 execute 在输入 run 下排他创建独立目录，不覆盖 PR 1 文件及任何旧判断：

```text
<run>/judgments/<judgment_id>/
  manifest.json       输入 trace 摘要、协议和配置身份、计划维度与预算
  tasks.json          完整最终题清单、实际材料与任务选择
  calls.jsonl         调用意图、可见响应／错误和任务完成事件
  judgments.jsonl     每题的派生校验结果与未评状态
  metrics.json        技术覆盖、原始模型结论、人工待复核及固定分母指标
  review_queue.jsonl  待人工的题／阶段及原因（不包含伪造裁决）
  report.md           人可读报告
  complete.json       最后写入的终态与必需产物摘要
  replays/<replay_id>/ 离线重算的新结果与报告
```

本修订使用新协议 `judge-v3`；协议身份包括 schema／rubric 版本、prompt hash、实际输入摘要、模型和安全端点身份、采样参数、输出 schema、上下文范围及预算。稳定审查 key 以这些真实内容计算；本批只记录，不自动跨运行复用。改变 input、模型或 rubric 必须产生新结果，不能覆盖旧文件。

调用前先持久化请求意图与实际冻结 messages；每次 `.create()` 有独立 call ID。调用完成后持久化可见响应或脱敏失败，再保存校验结果。意图不是服务已收请求证明；进程中断留孤立意图时，调用是否发生、费用和成功状态均未知。

JSON 文件采用同目录临时文件写入后替换；事件追加完整行并 flush/fsync。所有必需结果落盘后才写终态。记录写失败不得继续模型调用；不能保证故障磁盘上还写得出错误报告。与 PR 1 一样，不承诺多文件原子事务，也不能从完整文件推断原 CLI 必定退出 0。

离线复算读取并校验版本、身份、事件顺序、输入来源摘要、终态和声明的文件 hash，从 `calls.jsonl` 中 `call_finished.response.content` 重新进行 schema 与 evidence 校验，再重算 judgments 与指标；不信任派生的 `judgments.jsonl` 判定或旧 `metrics.json`。损坏／截断记录不生成“完整评测”结论；路径穿越、symlink、未知版本及不匹配 source trace 必须拒绝或明确 incomplete。源文件保持不变，复算输出新目录。

### 5.1 旧协议与迁移边界

`judge-v1`／`judge-v2` 的输出、报告和失败记录只读保留。v3 replay 对旧协议／旧实现 hash 明确拒绝并说明需要匹配记录的版本，不能静默改变旧结果。本批不提供自动迁移、重审、断点续跑；若对历史可见响应做新验证器的纯函数离线诊断，必须明确这是“旧响应在新规则下的解释”，不是 replay、更不是收到 v3 prompt 的新模型试跑，不写回历史产物。

除 protocol_version／protocol_digest 外，replay 核验 `checker_identity`（judge.py、runner.py、metrics.py 实现 SHA-256）；不匹配拒绝。同协议 replay 从原始可见响应重建模型结论、技术状态、诊断和人工队列，再重算统计，不能信任旧派生结论。每次输出独立目录。
原始 trace 本身必须通过 PR 1 `inspect_run`；没有完整终态、artifact 摘要不匹配或无法确定最终题目时，不启动 judge 请求。完整 `completed`／`empty` 可审查；生成失败不能通过选择一部分文件伪装成成功题集。

## 6 报告与指标

报告首屏和 CLI 明确显示：技术运行状态、记录完整性、选中任务完成数／选中任务数、完整保留题覆盖完成数／2N、模型三维全正向题数、待人工数量、技术错误／未选数量、调用与重试。后续逐题展示三维原判、模型理由、来源信息、可选摘录定位注释和人工复核原因。

- `evaluated_phase_count` 统计完整且结构合法的模型评审，不依赖证据定位、结论正负或人工状态。
- 三维模型标签直接按 model_review 统计；无完整响应另列 error/not_evaluated。
- `schema_version=3`（与其他 v3 产物一致，不另设 metrics_version）；`selected_phase_count`／`selected_evaluated_phase_count` 表示本次选中范围；`planned_phase_count`／`evaluated_phase_count` 表示全部保留题范围。
- **模型三维全正向**（`model_all_positive_count`）表示 answerable + material supported + citation supported；即使同时存在人工 pending，也照实计数。它是模型观点，不叫“自动确认通过”，不代替人工批准、发布门槛或 gold 的 human_verified。
- 人工 pending 按阶段／题目独立计数，不能与模型正向计数互斥；每条理由可追溯。review_queue.jsonl 为阶段级后续人工处理输入，包含 schema_version=3、item_id、phase、pending 状态、reasons、model_review 与 evidence_records；无待办则为空文件。本批没有裁决录入工作流。
- 完整保留题始终作为全量分母，未选中题不能被隐藏；另报选中覆盖。零分母为 N/A。若保留模型正向产出率，使用 min(模型三维全正向数,请求题数)/请求题数并命名为模型统计，不称质量获批率。
- 调用数、失败、重试、耗时、可获得 usage 与成本分别列出，未知不是 0。请求成功不等于完成评审，完成评审不等于模型正向。

所有报告注明“初步模型审查，尚未人工校准”；无真实人工裁决不得宣称已复核。模型与生成同源风险仍记录。

## 7 验收

既有 PR1 与调用／预算／持久化边界必须回归：完整输入、两请求隔离、prepare/replay 离线、明确 execute、独立配置、全局预算、重试固定、写失败停止、完整性和实现身份核验、敏感信息不记录、源文件不覆盖。

本次新增验收：

| ID | 要证明的行为 |
| --- | --- |
| V3-01 | 结构完整即 evaluated；unknown／负判／矛盾／来源疑点不覆盖 raw verdict、不从完成数剔除 |
| V3-02 | quote 可选；省略号、不匹配、空白、歧义只为 info，不因其 pending；exact/whitespace 定位仍准确 |
| V3-03 | 未知 chunk、错页／来源、正向引用的跨页疑点、缺来源证据、矛盾、unknown/partial 进入人工；清晰负判不因跨页本身自动进入 |
| V3-04 | 模型原判、技术状态、人工状态独立，完整正向+pending 同时可统计，不保留自动批准用语 |
| V3-05 | 选中一题完成显示 2/2 与全量 2/6，运行 completed；选中技术失败才 incomplete，中断仍 interrupted |
| V3-06 | schema 错误／截断不作完整评审，不由半 JSON 提取 positive；疑点无追分重试 |
| V3-07 | v3 replay 重建人工队列和统计；v1/v2明确拒绝且产物不变，prepare/replay 无模型请求 |
| V3-08 | 离线诊断保存的 54670453 两个响应：保留三维模型正判、2 个完整阶段，省略号只作注释，不冒称 v3 pilot |

测试证明工程契约，不证明模型准确性。真实调用、抽样校准和人工工作流另行确认。本轮不启动模型、不改 .env、不 commit/push。

## 8 SDK 依据

实现保持现有依赖。SDK 客户端关闭自动重试并设置超时，参数以 [OpenAI Python API reference](https://developers.openai.com/api/reference/python) 和本地锁定版本签名核对；`json_object` 只保证 JSON 模式，不保证业务 schema，仍须 Pydantic 严格校验，见 [Structured Outputs 官方指南](https://developers.openai.com/api/docs/guides/structured-outputs)。这些资料说明 SDK 行为，不构成选择 OpenAI 付费服务的决定；judge 服务仍须独立显式配置。
