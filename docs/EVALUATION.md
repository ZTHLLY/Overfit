# Overfit 生成质量评测与 Golden Set 使用方案

> 更新：2026-10-06 · 状态：PR1／judge-v1/v2 历史实现已离线验收；用户已试跑本地及 OpenRouter judge，尚无已校准质量基线。judge-v3 职责修订历史验收为 439 项测试；新增 challenge 入口已独立离线验收，当前全量 519 项测试通过。仅执行零调用 challenge prepare，未做真实 challenge 模型调用，校准未完成。
>
> 初次数据审计：2026-09-16；2026-10-05 复核题库哈希、代码状态及索引关键统计，HEAD 仍为 `9e68aa9`。
> PR1 历史实现未调用模型；随后用户运行了真实三题 trace 与回放，并授权删除冗余旧索引。历史 PR2 开发未调用真实 judge；随后用户手动试跑本地 27B（中断）、8B 与 OpenRouter；4096 输出预算解决截断，但 v2 将带省略号的摘录当成不可采纳。本次只离线修订 judge，不自动启动模型，不修改 golden、生成 prompt 或索引；不执行 commit/push。

**当前优先方向是直接评测真实 `mock` 输出：保存生成过程 → 代码检查引用 → 独立模型调用审查材料支持与可作答性 → 生成逐题报告。** 现有 47 题保留用于后续检索与答题诊断，不要求用户再制作一套标注数据，也不把这些诊断作为生成评测开工的前置条件。

与初版相比，调整的是实施优先级和首期范围，不是否定 golden set。阅读第 4、8、11、12 节即可了解当前最小方案；第 5–7 节保留数据协议和后续诊断指标。

### 文档分工

| 文档 | 维护内容 |
| --- | --- |
| 本文 | 整体目标、技术概念、评测范围与后续路线 |
| [EVAL-TRACE-SPEC.md](/Users/silver/Documents/github/Overfit/docs/specs/EVAL-TRACE-SPEC.md) | PR 1 的行为与数据契约、兼容性、失败规则、AC-01 至 AC-12 验收标准 |
| [EVAL-TRACE-PLAN.md](/Users/silver/Documents/github/Overfit/docs/plans/EVAL-TRACE-PLAN.md) | PR1 历史里程碑、测试矩阵与执行证据 |
| [EVAL-JUDGE-SPEC.md](/Users/silver/Documents/github/Overfit/docs/specs/EVAL-JUDGE-SPEC.md) | PR2 独立 judge、证据校验、预算与评分契约 |
| [EVAL-JUDGE-PLAN.md](/Users/silver/Documents/github/Overfit/docs/plans/EVAL-JUDGE-PLAN.md) | PR2 历史 v1/v2 离线验收、judge-v3 修订计划和执行证据 |
| [EVAL-CHALLENGE-SPEC.md](/Users/silver/Documents/github/Overfit/docs/specs/EVAL-CHALLENGE-SPEC.md) | 人工 control/mutant 独立执行入口、信息隔离、记录与非目标 |
| [EVAL-CHALLENGE-PLAN.md](/Users/silver/Documents/github/Overfit/docs/plans/EVAL-CHALLENGE-PLAN.md) | Challenge 入口实施与离线验收进度 |

先维护 spec，再按 plan 编码；实现时改变行为或字段，同步更新 spec、测试和 plan。PR1／judge-v1/v2 已实现；当前 judge-v3 职责修订已独立离线验收，以 PR2 spec/plan 的 v3 执行证据为准。真实生成已由用户跑通，但没有真实 judge 质量基线。

## 1. 先回答最重要的三个问题

### 现有格式够不够用？

**够启动第一轮 eval，不需要推倒重做。** 现有 JSON 已经包含题目、四个选项、正确答案、来源页和审核标记，可以直接支撑：

1. **检索评测**：输入题目后，系统能否找到标注的相关材料？
2. **选择题答题诊断**：模型在不同材料条件下能否选对答案？
3. **开发回归**：更换 embedding、chunking、检索策略或 prompt 后，哪些题变好、哪些变差？

但它**不是完整的产品质量评测集**。Overfit 当前产品是“从材料生成新题”，不是“回答已有选择题”。模型答对这 47 道题，不能证明它生成的新题准确、有价值、不重复。因此必须另外评测真实的 `mock` 输出。

### 需要修改什么？

**保留现有题目 JSON；先补生成过程记录和评测协议，不强制增加题目字段。** 最重要的不是换 JSONL 或换平台，而是明确：

- 使用哪个索引、什么版本的材料；
- 哪些材料真正送进了生成模型，原始输出如何变成最终题目；
- 哪些事情由代码检查，哪些由独立 judge 审查；
- 如何表达“不支持”“证据不足”和服务错误，而不是把它们都算通过。

Golden 的解释文本、精细证据、拒答题属于后续增强，按实际目标补，不阻塞本次生成评测。少量抽查用于校准 judge，不是要求用户给每道新题写标准答案。

### 先写代码，还是先决定技术方案？

**记录、引用检查、judge-v1/v2 与离线测试已有历史验收。本轮已按先冻结 v3 spec／plan 再实现和独立验收的流程完成：模型负责语义判断，程序负责记录与技术状态，疑点交人工；不再让摘录字符串匹配充当最终裁判。** 不先搭大框架，也不必等全部长期决策完成。

推荐顺序：

```text
固定生成请求与运行身份 → 保存真实输入、原始输出和最终题目
→ 代码检查引用 → 独立 judge 审查 → 输出逐题报告
→ 少量校准与争议复核 → 根据失败证据再优化生成或选材

后续独立路径：golden 检索评测 → 必要时增加 MCQ 答题诊断
```

本文件第 11 节列出了需要确认的决策，第 12 节给出建议实现拆分。

## 2. 我们手里的数据，实际是什么状态？

### 2.1 Golden set 审计

文件：[ifn580_eval.json](/Users/silver/Documents/github/Overfit/eval/ifn580_eval.json)。

| 项目 | 实际结果 |
| --- | --- |
| 格式 | 普通 JSON 数组，47 条记录，字段结构一致 |
| 唯一性 | 47 个唯一 ID；规范化题干没有完全重复 |
| 覆盖周次 | week01–week11 各 4 题；week12 为 3 题 |
| 类型 | basic 35；scenario 12 |
| 正确选项分布 | A 12、B 12、C 11、D 12 |
| 审核标记 | 47 条均声明 `human_verified: true` |
| 字段完整性 | A–D 选项和选项来源齐全；所有来源数组非空，页码为正整数 |
| 题干来源 | 32 题标 1 页、14 题标 2 页、1 题标 3 页 |
| 独立证据位置 | 题干来源共 61 个 `(pdf, page)`；加正确选项来源仍为 61 个 |
| 全量标注来源 | 包含所有错误选项来源后为 75 个独立页，涉及 15 个 PDF |
| 备注 | `notes` 均为空 |

题库 SHA-256：

```text
60d1455702a17df25119d4232b3ee2c95cda493cb255f4811d14de0107cb5a5f
```

**结论边界：** 以上是结构与一致性检查，不是重新逐题核实全部正确答案。`human_verified: true` 是已有标注的声明；本次没有替审核人再次背书。

### 2.2 当前应该用哪个索引？

初次审计曾发现两个容易混淆的索引；旧连字符索引已按用户授权删除。以下保留历史对比，当前仅使用下划线版本：

| 索引绝对路径 | 文档 / chunk 数 | 61 个题干标注页的元数据覆盖 |
| --- | --- | --- |
| `/Users/silver/Documents/github/Overfit/index/IFN580_machine_learning.db` | 36 / 1,385 | 61 / 61 |
| `/Users/silver/Documents/github/Overfit/index/IFN580-machine-learning.db`（已删除，历史记录） | 7 / 182 | 0 / 61 |

**当前唯一有效的 IFN580 索引为下划线版本 `IFN580_machine_learning.db`，36 文档／1,385 chunks。** 它也覆盖全部 75 个标注页；旧连字符版本的来源命名和材料范围与 golden 不兼容。旧版 0/61 不是说里面完全没有相关知识，而是严格来源匹配失败。

这里的“覆盖”只表示某个 chunk 的 `source` 相同，且页范围包含标注页。它**不是 Recall@k，不代表检索过，也不证明 chunk 保留了关键事实**。该索引有 37 个跨页 chunk，匹配时必须考虑 `page_end`。

历史审计中两个索引的 meta 都记录了 `bge-m3`、1024 维、chunk size 250、overlap 25、schema version 1，但**缺少 parser 字段及版本**。当前代码允许旧索引缺失某些 meta 字段；“能够打开”不等于“历史构建过程可完整复现”。首轮应标记 `parser: unknown`，不能用当前默认值假装它是旧索引的真实配置。250/25 在本项目中是近似 token 单位，切块长度按字符估算，不是真实 tokenizer 计数。

另外：

- 仓库 `/Users/silver/Documents/github/Overfit/courses` 当前没有 PDF。
- 标注文档写的 `/Volumes/PortableSSD/public/QUT/IFN580_machine_learning` 本次不可访问，未能检查原 PDF 页数、视觉内容和原文件是否变更。
- 因此可以先做**基于现存索引快照的诊断 baseline**；原始材料级的语义复核、重建和 parser 对比，需等材料恢复可访问。
- PR1 历史测试使用 `/private/tmp/overfit-eval-trace-venv`（Python 3.12.14），当时项目环境损坏；随后用户通过 `uv run` 重建 `.venv` 为 Python 3.12.13。PR2 用当前项目环境完成 371 项离线测试，不重新安装依赖。
- 用户真实 run `1f5d21a7-c742-43f2-9287-9b832379906a` 生成／保留 3 题，PR1 replay 为 `completed / complete`；PR2 对该 run 的 prepare 和 judge-replay 均为零调用、6 个任务未评，不构成语义通过证据。

### 2.3 现有代码提供了什么？

| 现有接入点 | 能复用的能力 / 边界 |
| --- | --- |
| [retriever.py](/Users/silver/Documents/github/Overfit/src/overfit/query/retriever.py) 的 `retrieve()` | query → embedding → 排序后的 chunk，适合检索 runner |
| 同文件的 `gather_material()` | 无 topic 时聚类选材；有 topic 时检索 + MMR；这才是 `mock` 的选材路径 |
| [store.py](/Users/silver/Documents/github/Overfit/src/overfit/storage/store.py) | 返回 `source/page/page_end/text/score`，可做页级评分；目前没有 reranker |
| [models.py](/Users/silver/Documents/github/Overfit/src/overfit/models.py) | `GeneratedItem` 是开放式 question/answer/source/page，不是 A–D 答题模型 |
| [generator.py](/Users/silver/Documents/github/Overfit/src/overfit/query/generator.py) | `mock_exam()` 生成新题；保留校验、重试和引用过滤，新增可选 recorder 记录每次调用与处理 |
| [cli.py](/Users/silver/Documents/github/Overfit/src/overfit/cli.py) | 新增 `mock --trace`、`eval replay`、`eval judge`、`eval judge-replay`；仍无固定题答题接口 |

`GenerationResult` 保留原有返回行为；开启 `mock --trace` 后，实际材料、各次可见请求/响应、失败和引用修复记录会写入独立 run 目录，并生成基础报告。**PR1 原报告语义状态保持 `not_evaluated`；已实现的 PR2 在独立目录中审查最终保留题，不改写原 trace。默认 prepare 也保持未评，只有显式 execute 才调用 judge。** 默认 `mock` 不创建 trace，不能从旧题卷反推生成时的真实材料。

特别注意：当前 `fetch_k` 只是预留候选池参数，结果仍按向量距离取前 `top_k`，**加大 `fetch_k` 本身不是 reranking 实验**。打开索引的 CLI 路径会探测 embedding 维度，因此 `status` 也不完全离线。

## 3. Eval 的技术概念，用这个项目理解

| 概念 | 在 Overfit 中是什么意思 |
| --- | --- |
| Eval（评测） | 固定题目、环境和规则，运行系统并可重复地评分，而不是“看起来不错” |
| Golden set | 人工核验过的参考样本；是评分依据，但仍可能有标注遗漏或歧义 |
| Corpus / index | 课程材料 / 材料解析、切块、向量化后的索引；不是 golden set 本身 |
| Test case | 一次固定任务及其参考标签，例如一条 MCQ，或一次“生成 10 道题”的请求 |
| Harness / runner | 负责加载样本、调用被测系统、保存过程、调用评分器和生成报告的程序 |
| Grader / metric | Grader 判断某条输出；metric 把判断汇总成正确率、命中率等数值 |
| Baseline | 未做本轮优化前的参考成绩；不是随机猜测，也不是预设及格线 |
| Regression | 改动后原本能做对的题变错；“总分没降”也可能掩盖局部退化 |
| Ablation（消融） | 只改变一个因素，观察变化，例如固定模型，只比较有无检索材料 |
| Grounding / faithfulness | 回答中的论断是否由实际提供的材料支持，不是看它有没有写引用 |
| Oracle context | 人为提供已核验的正确证据，跳过检索，检查模型是否能利用材料 |
| LLM-as-judge | 用另一轮模型调用按 rubric 评分；它不是绝对正确的裁判 |
| Dev / holdout | Dev 可以反复查看和调参；holdout 在方案冻结后才用于最终验证 |

适合用代码判定的东西，不必交给模型：选项相等、JSON 格式、页码范围都可以确定性评分。开放式题目质量再交给人工或经过校准的模型评委。这种分工也符合 [Anthropic 对代码、模型、人工三类 grader 的说明](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)。

**单元测试与 eval 不互相替代：** 单元测试证明匹配公式、数据读取和错误处理按预期工作；eval 衡量真实课程题目上的质量。当前已新增 trace、引用、回放、故障注入与 fake-client CLI 测试，但仍不能作为真实检索或生成质量证据。

## 4. 三条评测线：不要混成一个“RAG 总分”

### A 生成质量评测作为首期重点

```text
固定出题请求 → gather_material() → mock_exam() → 新题、答案与引用
                    └──────── 记录完整实际输入和生成过程 ────────┘
                                        ↓
                         代码引用校验 + 独立 LLM judge
                                        ↓
                            逐题证据、判定和问题报告
```

**题目不必逐字出现在课件里。** 合格的改写、比较或明确设定的假想场景都可以；要检验的是题目需要的知识和答案结论能否由材料支持，而不是新题与 golden 的文字相似度。

例如材料说“过拟合可能表现为训练误差低、测试误差高”，系统可以出“某模型出现这种表现，可能是什么问题”。但不能省略必要条件、擅自把“可能”变成“必然”，或依赖材料外的知识才能解答。

首期只聚焦两项语义判断，引用检查作为配套技术检查：

| 检查 | 执行者 | 检查对象 |
| --- | --- | --- |
| 引用有效性 | 代码 | 路径/页码是否属于本次实际输入材料；原始引用与修复后引用分别检查 |
| 材料支持 | 独立 judge 调用 | 题干的知识性前提、答案关键论断与必要推导是否有材料依据；所引用的具体片段是否支持对应结论 |
| 可作答性 | 独立 judge 调用 | 只给题干和这些材料，能否回答；有无缺条件、歧义、缺失图片或无法完成的推导 |

难度、教学价值、整卷覆盖和语义重复留待后续扩展，首期不以六维人工打分为开工前提。通过首期检查只能说明这些维度的结果，不能宣称“整套题质量已全面达标”。

**Judge 不是新的标准答案来源，而是可出错的审查器。** 模型语义标签保持逐项返回 `supported / partially_supported / unsupported / unknown` 的支持判断，以及 `answerable / not_answerable / unknown` 的可作答性判断，附关键论断、chunk ID、可选解释性摘录和简短理由。引用支持单列判断，不能用其他页有证据来掩盖引用错误。精确 schema、聚合和边界见 PR2 spec；后续改动需要更新协议身份。

- Judge 的材料范围以生成 trace 为准，不能悄悄从全部课件补齐缺失证据。若另做全课件复核，必须命名为不同检查；“课件中有”与“本次输入中有”分开报告。
- judge-v3 中 quote 可选。exact/Unicode 空白折叠匹配可添加原文 span；未找到、歧义或省略号仅作“未逐字核验”注释，不使评审归零、不单独触发人工。chunk ID 仍必需；不存在的 chunk、错来源页或正向引用结论的跨页疑点进入独立人工队列，保留模型原判和技术完成。定位不证明逻辑支持。
- 材料过长、截断、公式提取不清、证据不足时允许 `unknown`，说明限制；不能补知识硬判通过。Judge 若不能接收完整材料，应记录实际审查范围并将该次审查标为不完整。
- 生成调用与审查调用分开，judge 不读取生成模型“我已检查正确”等自评。独立调用不强制要求不同型号；若同型号，仍需记录同源偏差风险，不能宣称裁判因此独立可靠。
- 待评题目和材料只作为数据，不能覆盖 rubric。结构错误、截断与服务失败是技术 error；可解析的 unknown／partial、结论矛盾或实质来源问题是已完成评审，同时 human_review.pending。模型也可显式 human_review_required。清晰负判不必自动转人工；程序不改 verdict、不最终批准内容。
- 记录判定和证据依据，不要求或保存模型内部思维链。重试只按固定传输/格式策略，不因为“不通过”而反复请求直到通过。

修订通过离线验收后，应先用一题检查速度和输出可靠性；确认可用且用户同意后，再以少量固定出题请求做 pilot，例如一次全课程请求和一次 topic 请求，合计请求 5–10 道题；请求文本、选材预算、生成参数和费用上限先固定，不在跑完后挑好看的题。原始可解析题目、修复/丢弃记录、最终保留题都要保存，不能只留下通过检查的题。

不要求用户重新标注 golden 或逐题补标准答案。开发者/助手可以先做证据复核并整理争议，用户只需在需要时参与少量校准或边界确认；助手复核也是模型判断，不能冒称人工核验。尚未经人工校准的报告须标记为初步自动审查，不能当发布质量证明。

### B 检索评测作为后续定位工具

```text
golden.question → retrieve() → top-k chunks
                                  ↓
                 对照人工标注的 source/page 评分
```

- 主实验：query 只用题干，衡量直接语义检索能力。
- 补充实验：query 用题干 + 全部 A–D 选项，衡量 MCQ 辅助检索。两个结果分开命名，不能混合比较。
- 两种输入均不得加入正确答案标记、gold 来源页或 `notes`。
- 暂不需要生成 LLM 或 LLM judge，但查询需要 embedding 服务；已缓存的检索结果可以纯离线重算指标。
- 主实验建议一次取 top 10，再计算 k = 1、3、5、10 的前缀结果。固定索引和排序；存在同分时记录实际名次，并在后续实现中明确稳定 tie-break。

**什么时候用它？** 当生成评测暴露材料缺失或选材问题时，可以用这组固定题检查检索能力。但 `retrieve()` 不等同于全课程 `gather_material()`；检索分数不能替代真实选材 trace，也不作为首期生成评测的前置关卡。

### C MCQ 答题诊断作为后续扩展

若后续需要隔离模型知识与检索的贡献，再新增仅用于 eval 的 MCQ answerer；它不属于首期交付，也不要把现有 `mock_exam()` 当答题接口。

| 条件 | 模型获得的信息 | 主要诊断目的 |
| --- | --- | --- |
| Closed-book | 题干 + A–D，没有课程材料 | 看模型已有知识/猜测能达到什么水平 |
| Retrieved-context | 同样题干 + A–D + 实际检索材料 | 测固定题上的 RAG 答题表现 |
| Oracle-context | 同样题干 + A–D + 人工确认的最小充分证据 | 尽量隔离检索，诊断模型理解和答题能力 |

三组固定模型版本、答题 prompt 模板、采样参数、格式规则和重试预算，只切换 context 条件；不同条件自然会有不同材料长度，必须记录长度和裁剪策略。Oracle 是诊断参照，不保证一定最高分。

**现有 source/page 只能直接构造“标注页上下文”条件。** 若只是把覆盖标注页的 chunk 拼进去，尚未核实证据充分性，应命名 `annotated-page-context`，不要冒充已验证的 oracle。恢复原 PDF 后核验最小充分证据，再升级为 oracle。

如何解释结果：

- Closed-book 已很高：题目可能主要测到模型原有知识，不能把高分全归功于 RAG。
- Oracle 高、retrieved 低：优先查检索、上下文拼接、裁剪和噪声。
- Oracle 也低：查题目歧义、证据充分性、prompt 和模型能力。
- Retrieved 低于 closed-book：材料可能引入干扰；不能默认“检索一定有益”。

无 RAG 与有 RAG 的差值只是**这个固定实验中的增益**，不是所有真实使用场景的提升率。

## 5. 数据协议：哪些可以直接用，哪些不能直接推断

本节的题目字段规则适用于后续 golden 检索/答题路径；首期生成评测使用固定生成请求与实际运行 trace，不要求为新题预写 `correct_option` 或 gold 来源。两种任务共享版本记录原则，但不能混用输入 schema。

### 5.1 必须固定的字段语义

- `id`：稳定样本身份，不把行号当 ID。
- `week`：分析标签；`week01` 与真实 source 的 `week1/` 可以并存，不自动改写路径。
- `pdf`：固定 course root 下的完整相对 POSIX 路径。不要只比较 basename，也不要靠模糊匹配掩盖索引版本错误。
- `page`：1-based PDF **物理页码**。`printed_slide` 仅作辅助显示，允许 null，不参与主评分。
- `question_sources`：人工提供的题目相关来源，不保证是唯一或穷举的正确证据。
- `option_sources`：帮助核对每个选项的来源；错误选项对应的页可能用于反驳它，不能当作“支持错误选项为真”。
- `human_verified`：是否获准纳入正式评分。未来 false 的样本不混入正式分母，单独列出；本版全部 true。

**Golden 检索正例页集合的建议定义：** 每题 `question_sources ∪ option_sources[correct_option]`，按 `(pdf,page)` 去重。本版逐题并集不增加额外页，但把规则写清能兼容后续题库。

错误选项的来源不默认加入这个集合；否则系统检索到干扰项知识，也可能被错误奖为命中。反过来，不能把这些页自动当负例：它们也可能有助于辨析答案。

### 5.2 输入与评分标签必须隔离

| 去向 | 允许内容 |
| --- | --- |
| 生成模型 | 固定出题请求、实际选中材料及来源标签，不附 golden 答案或预期审查结论 |
| 生成质量 judge | 固定评审规则、该次实际材料、待评题目/答案/引用；不附生成模型自评或期待的通过结论 |
| 检索器 / query rewrite / reranker | 主实验只给 question；补充实验给 question + 全部 options；以及正常候选材料 |
| MCQ answerer | question、options、该实验条件允许的 context |
| 评分器 | correct_option、gold evidence、模型输出和运行记录 |
| Oracle 材料构造器 | 可读 gold evidence 选择材料，但不把答案字母、字段关系或“正确选项证据”等标签写进模型输入 |

普通检索材料保留 `source/page` 是正常 provenance，不是泄漏；**用黄金来源去引导普通检索**才破坏测量。Oracle 显式使用 gold 标签，必须作为独立诊断条件标注。

不能把整条 golden JSON 直接塞进 prompt；不能将该题库或包含其答案的 eval 报告一起 ingest 进检索语料。用于评分的参考答案也不能进入格式修复提示。

### 5.3 最小增补：旁路 manifest，不重写题目

建议新增的文件均为后续计划，**本次未创建**：

- `/Users/silver/Documents/github/Overfit/eval/generation_tasks.json`：首期固定出题请求、稳定 task ID、topic、题数与选材预算，不含新题标准答案。
- `/Users/silver/Documents/github/Overfit/eval/protocol.json`：首期 judge 输入/输出规则、指标版本、预算、超时与重试策略；不同评测任务使用独立配置，不能把检索 k 当作全课程选材预算。
- `/Users/silver/Documents/github/Overfit/eval/manifest.json`：后续 golden 路径的数据版本、文件 SHA、样本 ID、课程映射和页码协议。
- `/Users/silver/Documents/github/Overfit/eval/annotations/`：仅在后续需要时保存补充标注；不是首期需要用户填的文件。

运行结果另存 `/Users/silver/Documents/github/Overfit/outputs/eval/<run_id>/`，不要混进 gold 文件。每次运行保存 resolved config，不只是保存环境变量名。

必要的 run manifest 包含：

| 类别 | 应记录内容 |
| --- | --- |
| 数据身份 | 生成路径记录 task 清单及 SHA；golden 路径记录 dataset 版本/SHA；两者均记录协议版本/SHA、实际任务 ID 与用途 |
| 代码身份 | git commit、是否 dirty、相关未提交代码 diff/hash；本次 golden 尚未纳入 Git，单记录 HEAD 不够 |
| 材料与索引 | 显式 DB 路径、SQLite 一致性快照 SHA、documents hash 清单；原 PDF 可用时核对实际文件 SHA |
| 模型 | embedding / generation / judge 的模型 ID、可获得的版本或 digest、非敏感服务身份 |
| 构建与运行配置 | parser 身份和版本（未知写 unknown）、chunk 配置、依赖锁文件 SHA、请求题数和选材预算（检索另记 k）、context 顺序/预算/裁剪规则、生成与 judge 各自的 temperature、seed（若支持）、重试/超时 |
| 观测数据 | 起止时间、缓存命中、逐题耗时、token usage（若服务提供）、错误和调用次数 |

SQLite 有 WAL 时不能仅复制主 `.db` 并假定得到完整快照；应使用 SQLite backup 或在写入停止后做一致性快照。旧索引构建不可复现与同一快照上的检索可比较，是两个不同问题。

不要记录 API key 或完整 `.env`。模型别名不一定锁定模型权重；无法确定版本时把限制写进报告。流式 delta 数量不是 tokenizer token 数，不能拿它直接计算 token 成本。

### 5.4 何时才需要增加题目字段？

| 想测什么 | 应补什么 | 是否阻塞首期生成 MVP |
| --- | --- | --- |
| 生成题的材料支持与可作答性 | 记录实际输入和输出、judge 协议；不需新题标准答案 | 需要代码实现，不需要重标 golden |
| 选对 A–D | 现有 correct_option 足够 | 否 |
| 标注页检索代理指标 | 现有来源足够，先固定匹配协议 | 否 |
| Golden 检索的充分证据召回 | 最小知识点、必要证据与替代证据组、核验的短摘录/span | 否，仅影响该后续指标 |
| Golden 答题解释的参考答案评分 | reference_rationale 或关键事实清单，人工核验 | 否，仅影响该后续指标 |
| 无依据时拒答 | 独立 unanswerable 样本及审核依据；现有 47 题不能覆盖 | 否，首期不声称测到了该能力 |
| 完整教学质量和发布判定 | 扩展 rubric 与经过校准的审查流程 | 否，属于后续扩展，首期结论不得越界 |

证据组可以这样理解：同一知识点的课件页和教程页可能是**任选其一**；一道比较题的两个知识点可能**都必须找到**。当前平面页列表无法表达这种 AND/OR 关系。后续可新增 `required_facts`，每个 fact 下列 `acceptable_evidence_sets`：一个 set 内所有证据共同需要，不同 set 互为替代。先人工确认事实与证据，不让模型凭空补。

不要把当前 chunk ID 当永久 gold 主键：重切块后 ID/边界会变化；以材料版本 + source/page + 必要的文本 span 锚定，再映射到当前 chunk。

## 6 生成审查与后续诊断指标

### 6.1 生成质量 MVP 指标

judge-v3 不再提供程序“自动确认通过”。评审结果分三层：**模型三维结论、技术状态、人工状态**。这改变了历史 v1/v2 的统计语义，因此使用新协议，不能直接把旧通过率和新模型正向比例比较。

| 指标 | 当前口径 |
| --- | --- |
| 产出与调用 | 请求题数、原始/最终题数、修复/丢弃、调用尝试/成功返回、重试、usage与耗时分开 |
| 技术完成 | 完整且 schema 合法即完成，无论语义正负、定位注释或待人工；截断/传输/schema 错误另列 |
| 选中与全量覆盖 | 选中 1/3 题，完成可显示 2/2 选中任务、2/6 全量任务；未选题始终保留 |
| 模型三维结论 | 直接展示 answerability、material_support、citation_support 原值，不由代码重判 |
| 模型三维全正向 | answerable+supported+supported 的题数（同时显示全部保留题数）；若后续展示比例，分母固定为全部保留题。即使同时人工 pending 也照实统计，不是批准 |
| 人工待复核 | 模型 unknown/partial/显式要求、结论矛盾、实质来源疑点按题/阶段另列；含逐条原因 |
| 摘录注释 | 已逐字/空白核验、未提供、无法定位或歧义；未定位不等于语义错误，不单独触发人工 |
| 成本 | 生成与 judge 分开，未知费用/usage 不写 0 |

完整保留题始终计入全量分母；零分母比例 N/A。模型正向产出率若展示，使用 min(模型三维全正向数,请求题数)/请求题数，不是质量批准率。错误/未评不能伪造模型结论，人工 pending 不消除已有模型结论。原始及过滤后版本分别标审查范围，不借最终判断冒充全部版本已评。

运行 `completed` 仅表示**本次选中任务**都技术完成，负判或人工 pending 不改为 incomplete；选中任务因错误或预算等缺结果才 incomplete。原始生成失败仍保留请求分母。`review_queue.jsonl` 只导出人工待办，本轮不实现人工裁决录入，不声称已经人工审核；没有校准不能用模型正向比例宣布发布质量。

### 6.2 后续检索指标与标注页代理

令第 i 题标注正例页集合为 `G_i`，检索前 k 个 chunk 为 `R_i(k)`。一个 chunk 命中一个标注页，当且仅当：

```text
chunk.source == gold.pdf
且 chunk.page <= gold.page <= (chunk.page_end 或 chunk.page)
```

令 `H_i(k)` 是被这些 chunk 覆盖到的不同 gold 页集合。一个页被多个重叠 chunk 覆盖，只计一次。

| 指标 | 定义 | 如何理解 |
| --- | --- | --- |
| Page Hit@k | 平均 `1[H_i(k) 非空]` | 至少碰到一个已标注证据页的题目比例 |
| Known-page Recall@k | 平均 `|H_i(k)| / |G_i|` | 每题人工列出的页，找回了多少；按题宏平均 |
| Page MRR@k | 首个命中任一 gold 页的 chunk 排名为 r，则得 1/r；前 k 无命中得 0；按题平均 | 相关来源是否排得靠前；不是全证据充分性 |
| All-known-pages@k（辅助） | 平均 `1[H_i(k) == G_i]` | 所有已标注页都被覆盖；不等于逻辑上必须找齐它们 |

示例：某题标了 2 页，前 5 个 chunk 中只有第 2 个包含其中 1 页：Hit@5 = 1，Known-page Recall@5 = 0.5，MRR@5 = 0.5，All-known-pages@5 = 0。

各项均以运行前固定的合格题目清单为分母。实际空检索得 0；查询异常或超时也在主汇总中计 0，并单列基础设施错误，不把失败题删除。若额外报告“成功请求内的检索质量”，必须同时列成功数/计划数且不能替代主汇总；有基础设施故障的运行需先复核，不能把掉分直接归因于检索算法。

这套检索协议是为本项目定义的页级代理指标，不把它冒称为完整语义 Recall。排名、precision/recall 及相关性标注的基础可参考 [Stanford《Introduction to Information Retrieval》评测章节](https://nlp.stanford.edu/IR-book/html/htmledition/evaluation-of-ranked-retrieval-results-1.html)。

必须保留的限制：

1. 同一页有多个 chunk；命中页上的无关 chunk 也会得到代理分。因此要抽查实际文本，不能把页命中等同于证据支持。
2. 跨页范围更加粗糙；大 chunk 容易覆盖更多页。对比 chunk size 时同时报告上下文长度、延迟和必要事实覆盖，不只追逐页级分数。
3. 可能找到标注之外的等价正确证据。首轮计为“未命中已知页”，人工复核后再版本化修订 gold，不能直接断言检索错误。
4. 现有标签不是穷举相关材料，所以不把“未标注 chunk”当不相关来宣称严格 Precision@k；nDCG 等精细排名指标暂缓，待有可靠相关性标签再加。
5. 多页标注中有互补也可能有冗余，Known-page Recall 不是“模型可作答概率”。

### 6.3 后续选择题指标

主分数：`MCQ Accuracy = 最终有效且选对的题数 / 计划评测的合格题数`。

- 答题输出使用小型固定 schema，例如 `selected_option` 为 A/B/C/D 或 null；null 表示弃答。本集均可回答，弃答计 0。
- 若需要解释与引用诊断，再增加 `explanation` 和 `citations`，并固定所有对照条件的输出协议。
- schema 错误、超时、最终未返回有效选项均计主分数 0，同时单列错误率；不能静默删掉失败样本。
- 可以额外报告“有效响应内准确率”，但不得用它替代主分数。
- 重试仅按预先固定的传输/格式策略，记录首答与最终结果；**不能看答案错了就重试到对**。

同时报告总分、basic/scenario 分组、每周结果、逐题回归清单。均匀随机猜测期望 25%；本集永远猜 A、B 或 D 为 12/47 ≈ 25.5%。这些只是参照，不是可接受的产品分数。

### 6.4 正确性与引用支持的区别

- **Answer correctness**：是否真的答对？A–D 可以精确匹配，解释需要另评。
- **Faithfulness**：解释中的论断是否能从实际 context 推出？[Ragas 的定义](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/faithfulness/)以被材料支持的论断比例衡量。
- **Citation validity**：引用路径/页码是否属于实际提供材料？可以代码检查。
- **Citation support**：引用位置是否真的支持对应论断？需要证据语义检查。

只输出一个“B”时，不能对这个字母做有意义的论断级 faithfulness；答对字母也不能证明推理和引用正确。Ragas 的 [LLM Context Recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)需要可拆成事实的 reference，不能简单把 `correct_option: B` 当参考答案内容。即使使用正确选项的文本，也要确认它是否足以表达完整参考事实。

当前 generator 会过滤不存在于提供材料的引用，也会把相差不超过 2 页的引用修到允许页。这是产品容错，不是语义正确性保证。产品 eval 应保存原始输出和处理后输出，分别记录错误/修复/丢弃；gold 检索匹配不使用这种 ±2 页容错。

## 7. 如何划分与保护这 47 道题？

本节只约束后续 golden 路径；拆分题库或新增 holdout 不阻塞生成记录与自动审查的开发。首期生成任务也应固定版本，不能把反复调过的生成请求宣称为独立测试。

**推荐默认：先把这 47 题作为 v0 开发诊断与回归集，不宣称它是大型独立测试集。** 后续再增加独立审定的 holdout；第一轮重点是发现失败类型，而不是证明某模型普遍领先。

如果当前必须保留 test，可按证据/近重复组做大致 3:1 的 dev/test 划分，再尽量兼顾周次和题型；精确数量由分组结果决定，不机械抽 12 道。必须在用结果选择配置前冻结 ID 清单。已经用于调参的题不能重新命名成“未见过的 test”。

本次发现的分组风险：

- `week10-q03/q04` 共享题干证据页；`week12-q01/q03` 也共享。
- 考虑所有选项来源，还出现 week01、week06、week07 的额外重叠题组。
- 索引中 `week7/clustering.pdf` 与 `week8/clustering.pdf` 各有 119 种独立 chunk 文本，其中 **97 种完全相同**。两个文件 hash 不同，所以仅按文件 hash 或 week 分组仍不足以消除近重复。

因此应结合证据页、重复文本、考点和题意做分组检查。单靠随机 seed 不能防泄漏。

**知识材料在索引里不是天然泄漏。** RAG 本来就要读取课程资料；要防的是黄金题答案/标注进入候选系统、在 test 上反复挑配置，以及近重复题跨 split 制造虚假的泛化能力。

47 题中一题约 2.13 个百分点；每周只有 3–4 题。比较时展示配对变化：多少题从错→对、多少题从对→错，不能凭一两题差异宣称稳定提升。需要不确定性估计时优先按题目/相关题组做配对重采样，并说明样本小、题组不独立；同一题重复跑三次不等于增加三道独立试题。

## 8 实际执行顺序与交付物

### Phase 0 运行身份与开发环境（离线基础已就绪）

1. 保留 golden 原文件，不追加人工标注任务；为首期生成请求和评分协议设计版本记录。
2. 选定下划线索引的一致性快照，记录模型、chunk 配置及未知的历史 parser 身份。
3. PR1 历史离线测试使用隔离 Python 3.12.14；用户现已用 `uv run` 修复 `.venv` 为 Python 3.12.13，PR2 在该环境验收。真实 judge 仍需先确认服务和预算。
4. 定义 trace schema：task/run/item ID、实际输入材料及其顺序、完整生成消息、各次调用的配置/响应/错误、引用处理前后题目及关联关系。保存可见响应和调用元数据，不保存模型内部思维链或密钥。
5. 若原 PDF 仍不可用，报告明确标记“基于索引文本”；允许检查这些文本对生成题的支持，但不声称核实了原 PDF 视觉内容或解析保真度。

交付：可复用的 trace/manifest 数据契约与测试 fixture。不要重新检索来伪造过去一次生成的输入；没有完整 trace 的旧输出不能当作已重建的真实运行。

### Phase 1 保存生成过程并做代码引用校验（已实现）

1. 接入实际 `gather_material()` → `mock_exam()` 路径，完整落盘真实材料和每次可见响应，不改选材、出题 prompt 或生成参数来追分。
2. 对生成前后结果保留关联 ID；记录引用修复和丢弃，保留空结果、格式失败、超时及次数，而不只保存成功题卷。
3. 代码分别检查原始/最终引用的 source 与页范围。严格记录错误；不能把现有 ±2 页修复后的有效引用算成“原始引用就正确”。
4. 实现纯离线 trace 回放和基础报告，用 fixture 测试跨页、错误路径、页码缺失、空输出、不可解析响应、失败调用及重复运行不覆盖原记录。

交付：可复查的生成证据包、引用检查结果和离线测试。**到这一步还没有语义评分，不宣称题目被材料支持。** 这一批代码不依赖 judge 型号；用假客户端测试，不擅自触发真实模型调用。

### Phase 2 独立 judge 与逐题报告（v1/v2 历史验收，v3 已独立离线验收）

1. 确认 judge 型号/版本、端点、上下文限制、费用与调用上限、超时/重试以及第 4、6 节的最终评分协议。
2. Judge 从 trace 读取实际材料和待评输出，输出支持判断、可作答性、引用支持、证据摘录和理由；生成自评不作为评分依据。
3. 代码保存模型原判、技术状态和人工状态；证据定位仅辅助注释。结构完整即完成，unknown/partial、实质来源疑点及结论冲突进入人工；截断/结构/服务错误独立，不拿半 JSON 宣称正判。
4. 在固定的小批请求上跑 pilot，输出全部题目结果及错误，不自动挑高分。用户预算未确认前只用假客户端/fixture 验证流程，不发付费请求。
5. 开发者/助手先核对证据与异常，少量抽查用于校准；有争议的边界再交用户确认，不要求重新造一套标准答案集。明确区分模型复核和人工复核。

交付：逐题语义审查报告 + 固定协议版本 + 可重放的判定记录。未校准时注明“初步自动审查”；不把 judge 的分数当绝对真值。

Judge 的独立调用不要求接入 Ragas 或评测平台。PR2 只审最终保留版本，每题 answerability 与 support 两次独立请求；10 道保留题无重试即需 20 次请求。修复前和丢弃版本明确未评，不借最终判定冒充已评。全局调用预算包含重试，SDK 重试关闭；有效负判、证据问题与一致性冲突不重试。改变 judge prompt/模型/输入范围后必须产生新版本结果，不覆盖旧结果。

### Phase 3 按失败类型扩展诊断

只有当基线暴露问题或比较模型确有需要时，再按需增加：

- **Golden 检索 runner**：用现有 47 题测 k=1/3/5/10 的页级指标，定位已知考点检索；question-only 与 question+options 分开。实际全课程选材仍查 `gather_material()` trace，不能由题目检索分数替代。
- **MCQ answerer**：按第 4 节跑 closed-book、retrieved、标注页/oracle 对照。47 题 × 3 条件是 141 次基础生成请求，重复三轮为 423 次，另计重试；不是首期必须支付的预算。
- **完整产品质量**：逐步增加重复、覆盖、难度和教学价值，先定义清楚 rubric，再决定自动化程度；不混进首期支持率。

已有手动探针仍可使用，但不是新 eval 命令：

```bash
cd /Users/silver/Documents/github/Overfit
# 前提：环境已修复、embedding 服务可用，且配置与选定索引一致。
uv run overfit search "bias variance tradeoff" --course IFN580_machine_learning --top-k 10
```

当前没有名为 IFN580 的 DB，不从 golden 文件名猜索引。已实现 `overfit mock --trace` 和 `overfit eval replay RUN_DIR`：前者仍调用 embedding/生成服务，后者仅处理既有本地记录。未因此获得真实模型运行授权。

### Phase 4 回归与发布检查

- 纯离线 CI 覆盖 trace、引用、judge schema/证据校验、聚合分母与错误处理，不自动联网调用模型。
- 集成 eval 显式启用，固定生成请求、输入、索引、生成模型与 judge/protocol 版本；日志缓存键包含真实输入及这些配置，避免换协议后误复用旧判定。
- 对同一 trace 另开显式 judge 运行可以比较不同 judge 协议；judge-replay 仅同协议复算，不把旧响应冒称新 prompt 审查；固定 judge 再比较新的生成运行，才用于观察生成策略变化。两者分开报告。
- 不同策略生成的新题未必一一对应，不能像 MCQ 一样强行做“每题错→对”的配对；按固定出题请求比较，并展示具体输出。
- 发布门槛待基线与校准结果后再定，不拍脑袋指定通过率；严重错误单列，不用平均分掩盖。

## 9 每次运行应输出的报告

报告以实际生成任务为中心，不以 golden 答案正确率为主分数。PR1 提供运行、题目、引用与完整性事实，已实现的 PR2 旁路补充 judge 判定和语义指标：

1. **运行身份**：task/protocol/index/生成模型/judge/代码版本、材料范围、时间与未确认信息。
2. **运行与产出**：请求数/题数、原始解析情况、最终保留数、修复/丢弃、失败和重试。
3. **逐题依据**：题干、答案、原始/最终引用、输入 chunk、judge 判定、证据摘录及定位结果。
4. **汇总与待复核**：第 6.1 节的模型三维原判及全正向比例、技术覆盖/错误、独立人工 pending；不称自动批准，不隐藏分母或负判。
5. **后续动作**：按选材、生成、引用和 judge 本身的错误归因；只在对应证据足够时下结论。

已实现的 PR 1 产物位置如下（输出根尊重配置；有保留题才输出题卷/答案）：

```text
/Users/silver/Documents/github/Overfit/outputs/eval/<run_id>/manifest.json
/Users/silver/Documents/github/Overfit/outputs/eval/<run_id>/traces.jsonl
/Users/silver/Documents/github/Overfit/outputs/eval/<run_id>/result.json
/Users/silver/Documents/github/Overfit/outputs/eval/<run_id>/report.md
/Users/silver/Documents/github/Overfit/outputs/eval/<run_id>/<course>_mock_exam.md
/Users/silver/Documents/github/Overfit/outputs/eval/<run_id>/<course>_answers.md
/Users/silver/Documents/github/Overfit/outputs/eval/<run_id>/replays/<replay_id>/
```

`traces.jsonl` 保存原始运行事实，`result.json` 是派生汇总而不是终态提交标记。离线回放核验终态及文件摘要，并另写 replay 报告，不覆盖原记录。PR2 在 `judgments/<UUID>/` 保存 `manifest.json`、`tasks.json`、`calls.jsonl`、`judgments.jsonl`、`metrics.json`、`review_queue.jsonl`、`report.md` 和最后写入的 `complete.json`；`eval judge-replay` 从原始可见响应重验并写新 replay 目录，不信任旧分数。后续 golden 路径可独立增加 `predictions.jsonl`，按题 ID 展示配对变化和 week/type 分组；不要给新生成题冒用 golden ID。

建议错误类别包括 `source_unavailable`、`extraction_loss`、`context_truncation`、`unsupported_claim`、`unanswerable_question`、`invalid_output`、`citation_error`、`judge_error`、`infrastructure_error`。可能同时存在多种错误；无法定位根因时保留不确定性，不把所有失败都叫 hallucination。

## 10 框架与 judge 分开决策

**推荐轻量 Python + 现有 Pydantic + JSON/JSONL，并接入独立 judge 调用；暂不引入大型评测框架。** “不引入 Ragas”不等于“不用 judge”。引用结构可由代码判定，语义支持和可作答性通过独立审查处理，保留未来替换实现的接口。

后续需要统一的事实覆盖指标或更多批处理能力时，再评估 Ragas 等工具并核验所选版本的输入要求。不为框架重写 golden，也不为这轮 eval 迁移数据库、增加 reranker 或先重构整个生成器。先补观测，再依据实际失败优化。

## 11 实施范围与待确认决策

PR1／judge-v1/v2 已实现；用户已试用本地和 OpenRouter，judge 校准与发布门槛未确定。当前 v3 职责修订已实现并独立离线验收，不自行恢复模型运行。

| 项目 | 当前建议 | 确认时机 |
| --- | --- | --- |
| 首期范围 | 真实 mock 的材料支持、可作答性，以及配套引用检查 | 作为本版开工范围；不自动扩展难度/教学质量 |
| 首批代码 | trace 落盘、引用校验、离线回放与单元测试已实现 | 验收证据见 plan，不依赖 judge 型号 |
| Golden | 保留现有 47 题；检索/MCQ 属于后续独立诊断 | 不要求重标，不作为首期前置任务 |
| 索引 | 下划线 DB 的一致性快照；历史 parser 未知 | 首次真实生成前验证配置与运行身份 |
| Judge 型号与服务 | 独立调用；已试用本地 27B/8B 与 OpenRouter，可靠性尚未校准 | 接真实 judge 前确认；本地或托管均不擅自选 |
| 预算 | pilot 请求数、题数、最大调用数/费用及重试上限 | 任何真实评测调用前确认 |
| 评分协议 | judge-v3 已实现并独立离线验收；模型原判、技术状态、人工路由独立，见 PR2 spec | 变更协议产生新身份，不能边跑边改 |
| 校准 | 少量抽查/争议复核，不新增全面人工标注工作 | pilot 后检查 judge 可靠性，发布判定前必须说明校准状态 |
| 框架 | 轻量实现，可用 judge，暂不加 Ragas 平台 | 不阻塞首批离线代码 |

**v3 独立离线验收已完成（439 tests），下一步由用户决定新试跑与校准**，不是重做 golden。历史 v2 的 404 项测试和本次工程测试均不能证明模型可靠，也不自动赋予联网授权。保存响应的离线诊断显示 2/2 选中任务完整、全量 2/6、模型全正向 1 题、人工 pending 0；两处省略号仅 info。该诊断不改旧报告，不是新模型 pilot。

## 12 代码分批实施计划

PR 1 模块已位于 `/Users/silver/Documents/github/Overfit/src/overfit/evaluation/`，离线测试位于 `/Users/silver/Documents/github/Overfit/tests/`；PR2 的 judge.py、runner.py、metrics.py 已实现，PR3 及以后仍为后续工作。

### PR 1 生成过程记录与引用检查（已实现）

本批以 [spec](/Users/silver/Documents/github/Overfit/docs/specs/EVAL-TRACE-SPEC.md) 为行为契约、以 [plan](/Users/silver/Documents/github/Overfit/docs/plans/EVAL-TRACE-PLAN.md) 为实施清单。已提供显式 `mock --trace`，默认关闭；开启后使用 run 专属题卷/答案，不覆盖默认课程文件。`eval replay RUN_DIR` 已可用，具体数据和故障边界以 spec 为准。

- `contracts.py`：版本化 `Event`、`Manifest` 及专用读写错误；运行、调用和题目拥有独立身份。
- `live.py`：真实生成路径的 opt-in 生命周期、索引身份读取、产物发布与失败终态。
- `trace.py`：保存实际材料、完整可见消息/响应、调用错误及引用处理前后关联；不从输出猜原输入，不保存密钥或内部思维链。
- `citations.py`：严格 source/page/page_end 校验，原始/修复后结果分开，不改变现有生成语义。
- `report.py`：先输出运行事实、引用结果和明确的“语义未评”状态。
- `replay.py`：从持久化 trace 离线重算严格引用结果，不读取模型配置、原索引或 `.env`，不覆盖原始运行。
- 在真实生成入口增加记录钩子；离线假客户端覆盖成功、重试、格式失败、空结果、跨页、修复、丢弃、文件写入失败和同名运行保护。失败时在日志可写的前提下尽力保存终态；介质不可写则返回非零并保留不完整记录，不声称成功。

**验收：** 不调用模型服务即可跑测试；每道最终题可追到实际输入和处理历史；golden 不变。第一批不要求用户选 judge，也不宣称已有语义质量分数。

### PR 2 独立 judge 与评测报告（v1/v2 历史验收，v3 已独立离线验收）

- `judge.py`：独立配置/模型调用、结构化输出、证据定位校验、固定超时重试；缺模型配置时明确停在未评状态，不暗中使用默认付费服务。
- `runner.py`：从完整 trace 运行或重放审查，保存独立记录；稳定 key 包含真实输入和 judge/protocol 身份，本批不自动跨 run 复用缓存。
- `metrics.py`：分别聚合模型原判、技术完成与人工 pending；显示选中/全量分母，模型全正向不等于批准。
- `eval judge RUN_DIR` 默认纯离线 prepare；`--execute --max-calls N` 才读取独立 `JUDGE_MODEL/BASE_URL/API_KEY` 并调用服务；`eval judge-replay JUDGE_DIR` 纯离线重验。fixture 覆盖证据、schema、截断、未知、超时、指令干扰、预算和记录故障，验收证据见 PR2 plan。

**验收：** 完整模型原判不因摘录定位被抹掉；来源问题/矛盾/模糊进入人工、技术错误独立；不提供代码最终批准。replay 只支持匹配协议与实现，新旧产物不覆盖。真实 pilot 仍需用户确认。


### 独立 challenge 入口（已实现并独立离线验收）

[IFN580 人工挑战数据](/Users/silver/Documents/github/Overfit/eval/judge_challenges/ifn580_v1/README.md) 已独立验收：3 个未改 control、4 个单因素 mutant，共用原 6 个 chunks。已实现并独立离线验收 `overfit eval judge-challenge SUITE_DIR`，不是给正式 `judge RUN_DIR` 伪造 trace。完整 inputs/annotations 先严格校验，设计理由、预期目标、案例标识和配对类型只留本地报告，不进入 judge 请求。

```bash
# 离线准备
uv run overfit eval judge-challenge eval/judge_challenges/ifn580_v1
# 用户确认配置后手动执行；关闭重试时完整 7 例需 14 次请求
JUDGE_MAX_ATTEMPTS=1 uv run overfit eval judge-challenge \
  eval/judge_challenges/ifn580_v1 --execute --max-calls 14
```

可先 `--max-items 1 --max-calls 2` 做接通验证；前 1 条只有 control，不是变体检测实验。未选题仍可见，默认写当前工作目录 `outputs/eval/challenges/<UUID>/`，`--output-dir` 可改父目录。输出保存输入/注释快照、hash、原始可见响应、独立 challenge 覆盖、技术状态、人工队列及配对报告，不影响正式 judge 的指标或旧 replay 实现身份。

人工设计目标只是待检验假设，control 也不是人工确认的正确题。报告不自动计算检测成功率、期待标签命中率或准确率，不把模型不同意设计目标当技术失败。本轮无 challenge replay/resume；正式 `judge-replay` 拒绝 challenge 类型。本次独立验收 **519 tests**、Ruff/diff check、8 项额外禁网络边界检查通过，90 个 baseline 文件未变；正式 v3 原记录 6/6 阶段只读重建仍兼容。真实 suite [prepare 报告](/Users/silver/Documents/github/Overfit/outputs/eval/challenges/47c16ac8-faed-450b-ae21-6acf7365272e/report.md) 为 prepared、7 cases、0/14 已评阶段、0 次调用。工程验收和 prepare 不等于新模型 pilot 或语义校准，实际执行由用户另行决定。

### PR 3 小批校准与回归基线

- 固定全课程和 topic 请求，按预算跑小批生成与审查；保留全部输出。
- 对照实际材料抽查自动判定，整理分歧与边界；记录是人工核验还是助手复核，不混淆标签。
- 冻结经复核的协议版本与首份基线报告；发现 judge 错误先修评分器，不能为追分改 gold 或只留下好题。

**验收：** 能区分产品错误与评测错误，明确基线的适用范围和校准限制，不把初步自动通过率当成发布质量认证。

### 后续独立 PR

需要时再添加 GoldenQuestion loader、页级检索指标和 runner；随后按需增加 MCQ answerer 或完整教学 rubric。沿用第 5–7 节的数据隔离和评分规则。这些不是 PR 1、PR 2 的前置依赖。

## 最终建议

**不重做 golden，不要求用户再进行一轮完整人工标注。先把真实生成过程记录下来，再用代码引用检查与独立 judge 回答“这些题的依据在哪里，条件够不够”。**

judge-v3 的目标不是让所有题自动通过，而是让模型承担语义评审、程序保留真实技术记录、人工处理实质疑点。本次实现与独立离线验收已完成（439 tests），未新增模型调用、在线 pilot 或人工校准；默认 prepare 与同协议 replay 仍离线，旧 v1/v2 保留且明确拒绝不兼容回放，不静默重判历史记录。没有新授权不启动 judge；优化依据具体证据，不根据单个正向比例追分。
