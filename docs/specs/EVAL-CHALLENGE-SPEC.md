# Judge Challenge 执行入口 Spec

> 版本：0.1 · 日期：2026-10-06 · 状态：已实现并通过独立离线验收（519 tests）；仅执行离线 prepare，未调用真实 challenge 模型或完成语义校准。
> 用户已授权文档后实现；本轮不调用模型、不改数据集或历史结果、不 commit/push。

实施见 [EVAL-CHALLENGE-PLAN.md](/Users/silver/Documents/github/Overfit/docs/plans/EVAL-CHALLENGE-PLAN.md)，数据见 [ifn580_v1 README](/Users/silver/Documents/github/Overfit/eval/judge_challenges/ifn580_v1/README.md)。正式 trace judge 契约仍见 [EVAL-JUDGE-SPEC.md](/Users/silver/Documents/github/Overfit/docs/specs/EVAL-JUDGE-SPEC.md)。

## 1 目标与非目标

给现有人工 challenge 一个**独立试验入口**，观察相同 judge 对原题 control 和单因素 mutant 的反应。现有 suite 为 3 controls + 4 mutants，共 7 cases、6 个共享 chunks。它不是实际生成 trace、正式 golden 或已校准真值。

复用 judge-v3 的 rubric、请求隔离、客户端和响应验证，不另造一个打分器。不改变 `judge.py`、`runner.py`、`metrics.py`，避免破坏现有正式 judge-replay 的 checker_identity 兼容性。新入口与正式评审使用独立记录类型、目录和计数，不伪造源 run、manifest 或事件来绕过 trace 校验。

本批不实现 challenge replay/resume、自动缓存、并行请求、自动“预期命中/检测成功率/准确率”、人工裁决录入、通用评测框架或新数据设计。 annotations 中目标仅是人工设计意图，不是期待模型必须输出的硬编码标签。

## 2 CLI 与执行边界

```bash
# 离线准备：不读 .env、不加载 JudgeSettings、不初始化客户端、不发请求
uv run overfit eval judge-challenge eval/judge_challenges/ifn580_v1

# 用户自行确认端点、模型、预算之后才显式执行
uv run overfit eval judge-challenge eval/judge_challenges/ifn580_v1 \
  --execute --max-calls 14

# 可选：只执行最前 N 个 cases，其他案例仍在报告中标记未选
uv run overfit eval judge-challenge eval/judge_challenges/ifn580_v1 \
  --execute --max-items 1 --max-calls 2
```

`--output-dir PATH` 为输出父目录，默认当前工作目录下 `outputs/eval/challenges`；每次在其下排他创建 UUID 子目录。不得覆盖 suite、正式 trace、旧 judgment 或既有 challenge 目录。`--max-items` 按 inputs 的 case 顺序取前 N 个，不按设计类型挑样本，不自动补 control；若配对 control 未选中，报告如实标明没有本轮模型结果。execute 必须显式正整数 `--max-calls`，max-items 若设置也须为正整数。

每 case 固定 answerability、support 两个阶段，无重试 7 cases 共 14 次调用；全局预算包含失败和重试。SDK 隐式重试关闭，既有 judge 固定重试策略不变（默认每阶段最多 2 次尝试）；若设置 JUDGE_MAX_ATTEMPTS=1 则禁用 runner 重试，不因负判、unknown、pending、来源诊断或“不符合设计目标”追分重试。输入超限不静默截断。显式 execute 才加载独立 `JUDGE_*` 配置；不继承生成模型配置。

运行状态遵循技术含义：prepare 为 prepared；选中阶段都有完整结构化响应为 completed，即使模型负判或待人工；选中阶段因错误/预算/输入限制未完成为 incomplete；中断为 interrupted。未选 case 不令运行 incomplete。退出码 prepared/completed=0、incomplete=1、interrupted=130；无效 suite/配置或记录拒绝为非零，不冒充已完成评审。CLI 输出 challenge 类型、选中/全量覆盖、模型结果摘要、技术错误、人工待复核和报告绝对路径。

## 3 完整输入先校验，设计信息不进入模型

在读取配置或创建客户端之前，读取并严格验证整个 suite，而非仅验证 --max-items 选中的前缀。输入的两个文件使用现有 schema：`overfit.judge_challenge_inputs.v1` 和 `overfit.judge_challenge_annotations.v1`。不接受额外字段、错类型、bool 冒充页数、重复 JSON key、重复 case/chunk ID 或不支持的 schema。

- 两文件 suite_id 一致、cases 一一对应；共享 chunks 和 case.input 使用显式字段白名单与严格类型，页数为合法正整数，page_end 若提供不得小于 page；case 的 source/page 必须存在于共享 chunks 的来源/页范围。annotations 的 case 顺序可不同，但 ID 集合必须完全相同。
- provenance 仅本地审计，格式严格验证；不追随路径补材料，不依赖源 tasks 仍在原机器才能执行独立快照。源路径和 hash 的声明不代表本次重新验证源产物真实性。
- control 必须引用自身，changes 为空；mutant 必须指向存在的 control，并对应同一 source_item_id，不能互相环指或引用 mutant。
- changes 的 field 不重复，before/after 类型与 input 字段一致、before 等于 control 当前字段、after 等于 mutant 当前字段；记录变更必须与两条 input 的完整实际差异集合相等，不允许漏记或多记。
- 单因素范围仅当前数据所需，实际差异字段集合只允许 {question}、{answer}、{source}、{page} 或 {source,page}，source/page 作为同一个 citation 因素。未变字段必须相同，topic 不作为本批变异因素。
- qualitative_target 维度枚举、设计理由和标签政策严格解析但不判断其“正确”；保留 annotation_status 为人工设计未校准，不把 control 当正确真值。

只通过显式 adapter 构造发送字段：

| 阶段 | 允许给 judge 的 case 字段 |
| --- | --- |
| answerability | question + 完整共享 chunks |
| support | question、answer、最终 source/page + 完整共享 chunks |

既有固定 rubric 和输出 schema 正常发送。不得发送 annotations、case ID、suite ID、control/mutant 类型、provenance、source_item_id、变化记录、设计理由、预期目标、topic、兄弟案例、上一阶段响应或上一条案例响应。不能把整个 inputs.json 序列化发送。每阶段是独立 messages。

## 4 结果职责与持久化

复用 judge-v3 原模型结论、technical_status、human_review 和可选证据定位诊断。摘录省略号不能抹去模型原判，实质来源疑点/模型不确定/矛盾可进入人工；本入口不放宽或加严正式 rubric。人工状态不是设计目标是否命中的代名词。

每次独立 UUID 输出：

```text
outputs/eval/challenges/<UUID>/
  inputs.json          执行输入快照
  annotations.json     仅本地审计/报告的设计快照
  manifest.json        独立 record_kind、版本、快照 hashes、协议和安全配置身份
  calls.jsonl          调用意图、冻结请求、可见返回/安全错误与阶段完成事件
  judgments.jsonl      所有 cases 的模型原判、技术和人工状态（含未选）
  metrics.json         独立 challenge 覆盖与状态计数，不混入正式指标
  review_queue.jsonl   阶段级人工待办，无人工裁决标记
  report.md            可读逐条与配对对照
  complete.json        最后写入的终态及必需产物摘要
```

新产物使用独立 `schema_version: 1`、`record_kind: judge_challenge`、协议 `challenge-v1` 与 `challenge_run_id`；保留 suite schema/version 和复用的 judge protocol/rubric 身份。calls 事件使用 case_id，而非伪装 item_id/source_run/retained 统计。inputs.json 与 annotations.json 按原始 bytes 快照（保持它们原有 v1 schema），hash 对应这些确切 bytes。其余结构化产物按新类型版本保存。不得借用正式 run 身份。模型 key/headers/原始异常 repr/内部 reasoning 不保存；只存安全白名单配置、可见 content、usage 与时延。annotations 虽不入模型，但在本地快照与报告保留，帮助人解释观察结果。

调用前写 intent 并持久化，写失败则不调用；后续任何记录写失败停止继续请求，不以成功状态遮掩缺失日志。孤立 intent 只说明计划请求，不能证明服务成功或消费已发生。中断尽力保存已返回阶段及 interrupted 终态；磁盘不可写时不承诺能写出完整报告。完整 JSON 原子替换、JSONL flush/fsync；complete.json 最后写，不能从报告存在就认定记录完整。

本轮记录供本地查看与未来协议演进，不提供 challenge-replay。正式 `eval judge-replay` 应明确拒绝 challenge record_kind，不能将其伪装成真实 trace 的模型审查。

## 5 报告与对照口径

独立 metrics 字段包括 case_count、selected_case_count、control_count、mutant_count、selected_phase_count、selected_evaluated_phase_count、evaluated_phase_count、planned_phase_count、model_all_positive_case_count、complete_model_review_case_count、technical_error_phase_count、human_review_pending_case_count／phase_count、dimension_counts、call_starts、response_count、retry_count 与 usage；另记录 call_finishes、unresolved_calls、cost 和人工原因分布，未知成本不填 0。不借正式 retained 指标解释这些数据。

首屏清楚显示“人工设计挑战／尚未校准”，suite身份、全部/选中 case 数、两个阶段覆盖、请求/返回/重试、技术错误和待人工。逐 case 显示三维模型原判、原因、来源诊断和技术/人工状态；没跑的明确未评，不删除。

独立配对章节将 control 与 mutant 并排展示输入变化、设计意图及本次真实模型结论。设计目标明确标为“人工设计目标，非模型结果/校准真值”。control 若未执行就显示未评，不补结论。程序可以描述两个原始 verdict 的差异，但不得据此自动宣布“检测成功”或计算准确率、预期标签匹配率、正负例分数。模型全正向若保留，只叫模型意见统计，不叫通过率或实验成功率。

## 6 验收矩阵

| ID | 离线证明 |
| --- | --- |
| C-01 | 合法 suite 全量严格解析、配对和 before/after/单因素一致；坏后缀也在配置/客户端前拒绝 |
| C-02 | prepare 不读环境/配置、不初始化客户端、不联网；输入与历史产物不变 |
| C-03 | 两阶段请求只含许可字段；annot/provenance/case标识/期待目标/兄弟案例和上阶段结果均隔离 |
| C-04 | max-items 前N但其余可见；全局预算含重试；负判/pending不追分；SDK无隐藏重试 |
| C-05 | 截断/schema/传输与模型负判分开；选中技术完成和全量覆盖独立 |
| C-06 | intent写失败不调用，后续写失败停止；中断可诊断，安全配置/内容不泄露secret/reasoning |
| C-07 | UUID记录独立、快照/hash/终态齐备、不覆盖；正式judge-replay拒绝challenge；本轮不提供replay/resume |
| C-08 | 报告逐题/配对展示设计目标和原始模型结果；无自动预期命中或准确率、无伪造人工裁决 |
| C-09 | 既有全部测试回归；judge.py/runner.py/metrics.py哈希不变，正式replay兼容保留 |

作者测试与独立 reviewer 分开；测试仅 fake client 和禁网络 fixture。最终按真实证据回填计划，不将工程验收或 prepare 说成已跑通真实 challenge。
