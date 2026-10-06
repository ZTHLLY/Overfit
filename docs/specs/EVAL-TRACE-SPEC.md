# Eval 生成记录与引用检查 Spec

> 版本：0.2 · 日期：2026-10-05 · 状态：已实现并通过离线验收。
> 范围：总体 Eval 方案中的 PR 1。实现、fixture 测试与独立复核已完成；未进行真实模型 pilot，不代表语义质量已验证。

总体方向见 [EVALUATION.md](/Users/silver/Documents/github/Overfit/docs/EVALUATION.md)，实施顺序见 [EVAL-TRACE-PLAN.md](/Users/silver/Documents/github/Overfit/docs/plans/EVAL-TRACE-PLAN.md)。本文是这一批代码的数据和行为契约；plan 引用验收编号，不另外定义冲突的行为。

## 1 目标与交付边界

让一次真实 `mock` 生成成为可复查的记录：每道最终题都能追溯到当时的输入材料、可见模型响应和引用处理过程。即使调用失败、输出为空或题目被全部丢弃，也能区分事实与未知信息。

本批交付：

- 显式开启的生成 trace，使用本地文件，不引入遥测服务。
- 真实材料、完整可见请求、每次生成调用及其响应/错误记录。
- 引用处理前后题目关联，以及独立的严格引用检查。
- 不依赖模型、索引或原 PDF 的离线回放与基础报告。
- 使用假客户端、临时目录和合成材料的单元/CLI 测试。

本批不做：LLM judge、语义支持打分、MCQ answerer、golden 检索 runner、Ragas 接入、生成算法或 prompt 优化、重建索引、补标注、自动修复题目。所有语义评价统一标为 `not_evaluated`，不输出“内容正确率”或“材料支持率”。

**开发者负责写自动记录/检查能力，不是让用户手动记录每次运行。** 现有 47 题 golden 不读取为生成输入、不改写，也不新增用户标注任务。

## 2 当前实现与需要保留的行为

以下为实施前 HEAD `9e68aa9` 的基线与本批兼容约束；本批实现仍在工作区，未 commit/push：

| 文件与接入点 | 当前事实 | 本批约束 |
| --- | --- | --- |
| [cli.py](/Users/silver/Documents/github/Overfit/src/overfit/cli.py) 的 `mock()` | 打开索引、选材、调用生成器，写题卷/答案 Markdown | 未开启 trace 时保留原路径和行为；开启时增加可审计流程 |
| [retriever.py](/Users/silver/Documents/github/Overfit/src/overfit/query/retriever.py) 的 `gather_material()` | 无 topic 聚类选材；有 topic 检索 + MMR | 不换选材策略，记录真实结果与顺序 |
| [generator.py](/Users/silver/Documents/github/Overfit/src/overfit/query/generator.py) 的 `_complete()` | 外层校验重试，内层 response-format fallback | 不能把外层 attempt 当实际 API 调用数 |
| 同文件 `_stream()` | 读取可见 content；reasoning 字段只用于进度计数 | 记录可见 content，绝不把 reasoning 字段写入 trace |
| 同文件 `_drop_invented_citations()` | 可修复相差不超过 2 页的引用；也可能丢弃题目 | 保存原值、结果与动作；本批不改变该策略 |
| `GenerationResult` | 返回最后一次 raw、attempts、dropped、最终 exam | 保持已有调用方兼容；新记录不能只靠这个摘要补出来 |

已有引用检查只能判断页码是否合法，不能证明原文支持答案。原始 JSON 经 `_extract_json`、`_coerce` 和 Pydantic 处理后才形成题目对象，**处理前引用快照指“通过 schema 校验、尚未执行引用策略的题目”**，不是逐字原始 JSON；后者必须另存可见 raw 响应。

## 3 推荐的接口与兼容性决策

以下决策已按本契约实现；若改变，先更新本 spec 和 plan。

### 3.1 显式启用 trace

已新增 `mock --trace`，默认关闭；`eval replay RUN_DIR` 仅回放本地记录。

```text
在线入口（本轮未执行真实模型）：overfit mock --course COURSE --questions N [--topic TEXT] [--material N] --trace
离线入口：overfit eval replay RUN_DIR
```

- 不带 `--trace`：现有文件名、输出位置、生成请求、重试与引用处理逻辑保持兼容，不创建 eval 目录。
- 带 `--trace`：输出到 `settings.outputs_dir/eval/<run_id>/`，不会覆盖旧的课程题卷。题卷/答案沿用原 basename，在 run 目录内保持现有相对链接有效。
- 仅开启 trace 不增加生成请求次数，不调用 judge；正常 `mock` 本身仍可能调用 embedding 和生成服务。**只有 fixture 测试和 replay 是无模型运行。**
- `--questions` 必须大于 0；`--material` 必须非负，0 仍表示现有的默认预算计算。Trace 路径中的新输入校验在任何服务调用前完成；本批不顺便改变未开启 trace 的既有无效参数行为。
- 不提供“遇到记录失败就悄悄关闭 trace”的降级。显式请求审计后，不能生成一份看似已审计而记录不完整的成功结果。

### 3.2 输出与运行身份

默认输出根在本工作区对应 `/Users/silver/Documents/github/Overfit/outputs/eval/`，但实现必须尊重配置，不能硬编码该绝对路径。

每次生成新建随机 UUID `run_id`，目录排他创建；碰撞重试新 ID，不覆盖或接续旧运行。课程名不能控制任意文件路径。单次 run 内的逻辑 task ID 与 run ID 分开：前者可复用以比较同一请求，后者标识一次实际执行。

```text
<configured outputs_dir>/eval/<run_id>/
  manifest.json              初始运行身份和配置
  traces.jsonl               追加事件，原始事实的主要依据
  result.json                生成、引用检查和计数的派生快照
  report.md                  基础报告，明确语义未评
  <course>_mock_exam.md       仅有最终题目时生成
  <course>_answers.md         与题卷配套
  replays/<replay_id>/        后续回放报告，不覆盖原记录
```

Trace 是本地课程内容副本，不能自动提交到 Git 或上传到外部服务。本批不实现自动清理或云同步。

## 4 数据契约

以下为字段语义，不强制实现最终类名；使用现有 Pydantic 做落盘边界校验，不新增运行时依赖。

### 4.1 身份与版本

所有机器产物带 `schema_version: 1`。`run_id` 全程一致。事件 `seq` 从 1 严格递增，不能用时间戳代替顺序；时间戳用 UTC RFC 3339，耗时用单调时钟记录。

每次 `client.chat.completions.create()` 客户端调用尝试有唯一 `call_id`，另记外层 `attempt_index` 和本次 `format_index`，都从 1 开始。正常运行的 `call_count` 指该方法调用次数，不把 embedding 混进去，也不保证服务端已收到或计费；调用后报错仍计一次。测试中的假客户端验证相同计数语义，不是线上调用证据。

`call_started` 是持久化的调用意图与请求快照，不是“请求已发送”的确认。它落盘后、方法进入前也可能发生硬 kill。回放分别展示 start 数、finished 数及悬而未决的调用；有孤立 start 时，精确 `call_count` 为 null，并说明实际调用是否发生未知，不能拿 start 数冒充已确认调用数或费用。

通过 schema 的题目按成功 `call_id` 与原始 item 序号分配稳定 `item_id`。相同文本的两道题也不同 ID。修复前后沿用同一个 item ID，另存 `pre_policy`、`post_policy` 版本及最终序号；被丢弃的题保留 ID，`post_policy` 为 null。Schema 失败的响应不凭猜测提取题目，raw 仍保留。

### 4.2 Manifest

必需记录：版本、run/task ID、启动时间、course/topic、请求题数、选材预算、代码身份与允许记录的配置。代码身份至少包括可获得的 git HEAD、dirty 状态、相关代码/模板及锁文件哈希；不可获得时填 null 并说明原因，不读取整个工作区或复制私密文件。

只允许配置白名单：生成/embedding 模型名、脱敏服务身份、temperature、request timeout、max attempts、已知索引 profile。服务地址剥离 userinfo/query/fragment；不记录 API key、Authorization header、完整 `.env`、整个 Settings 对象或原始客户端对象。

初始 manifest 写入时可能还没有索引信息。之后通过事件记录实际索引路径、meta、在同一读取事务中获得的 documents hash 清单及其摘要；不要在初始化记录前为了这些字段隐式调用 embedding。历史 parser 或模型 digest 不可知时填 null/unknown。

**材料记录是回放生成输入的依据。** 索引身份摘要不等于 SQLite 全量快照 SHA；只有另行提供了已验证的一致性快照才填写快照哈希。正式基线所需的索引快照准备见总体方案，本批不会自动重建或复制整个索引。

### 4.3 输入材料与请求快照

`materials_selected` 保存有序 chunk 列表：`id/text/source/page/page_end/section`，缺省值显式为 null；另存选材模式、请求/实际数量和列表内容摘要。页范围必须合法，不能静默修复索引元数据。材料仍须按生成时的实际顺序传入。

每个 `call_started` 保存**本次准备传入 create 的冻结请求**：可见 messages、model、temperature、stream 标志、response_format 和有效 timeout。若调用实际发生，必须与发送内容一致。若消息含前次错误响应或修复提示，也按实际值记录；不能只存初始 prompt 或重新渲染后声称等同原请求。记录与真正发送共享同一冻结快照，避免后续列表变更篡改旧事件。

模板哈希有助于识别版本，但不替代渲染后的完整 messages。选材文本和 prompt 都不擅自裁剪；存储空间不足视为记录错误，而不是截断后标记完整。

### 4.4 事件类型

每条 JSONL 记录包含 `schema_version/run_id/seq/event_type/time/payload`。实现的最小事件集：

| 事件 | 核心内容与含义 |
| --- | --- |
| `run_started` | 初始任务身份；写入成功后才允许打开索引或调用服务 |
| `materials_selected` | 实际索引身份、有序材料与选材参数 |
| `call_started` | call/attempt/format 身份及完整可见请求，表示调用意图；持久化成功后才发请求 |
| `call_finished` | completed/error、完整或部分可见 content、响应完整性、耗时、脱敏错误类别；可获得的 usage，否则 null |
| `validation_finished` | 指向 call ID；valid/invalid/empty_body 与脱敏错误说明；不混淆空字符串和有效空 items |
| `citation_processed` | item ID、pre/post 快照、实际 keep/repair/drop 动作、实际原因及严格引用检查 |
| `run_finished` | 最终状态、阶段、计数与必需产物列表/摘要；唯一的正常终态提交标记 |

网络/流异常也有 `call_finished`，保留已收到的可见部分且 `response_complete: false`。正常流结束后拿到空字符串是空响应；`{"items": []}` 是可解析空结果，二者不同。

不要求每个 token 落盘：正常失败时从已收集的 content 写出部分响应；突然断电/强杀时，未持久化的内存片段可能丢失，必须标为不完整，不能编造回复。reasoning/reasoning_content 字段不保存；现有进度计数可保留，不把 delta 次数当 token usage。

## 5 引用检查与题目关联

独立严格检查器是纯函数，只读取题目和本次材料。规则：source 字符串精确匹配，且 page 是正整数并落入匹配 chunk 的闭区间 `[page, page_end or page]`。不改写大小写、不只比较 basename、不使用 ±2 页容错。

建议返回：`valid`、`unknown_source`、`page_not_supplied` 或 `invalid_page`，及命中的 chunk ID 列表。空材料无法形成有效引用；重复或重叠 chunk 不增加同一题的分母。同一 source 的不同 chunk 页段不能被合成含空洞的大区间。

引用校验对象为 schema-normalized 题目快照；原始 JSON 中被 Pydantic 转换的类型仍可通过 raw 响应查到，不宣称 pre_policy 保存了未经转换的原始字段。

先保存 pre_policy 并严格检查，再让现有引用策略处理，最后保存实际 post_policy 与严格检查。正常修复后，原始“无效”不会变成历史上的“有效”。丢弃题的 `post_policy/post_check` 编码为 null，语义是“不适用”，不把 null 当通过。

必须在现有处理循环处捕捉 keep/repair/drop 与 item mapping；不能仅靠最终文本 diff 猜测被删的是哪一道题。等距候选页的原有选择行为不在本批修改；若未来要确定 tie-break，应单列行为变更和测试。

这里的 `valid` 只表示引用来自材料。跨页 chunk 不能证明摘录属于哪一具体页；本批没有语义裁判，所有材料支持、具体页语义支持和可作答性字段均为 `not_evaluated`。

## 6 生命周期与错误行为

### 6.1 正常执行与提交

1. 校验 trace 参数，排他创建 run 目录，写入 manifest 与 `run_started`。
2. 打开索引/选材，写 `materials_selected`。写入失败时不进入生成调用。
3. 对每次真实生成请求，先持久化 `call_started`，请求后写 `call_finished`、必要的 validation 事件。
4. schema 成功后记录引用处理过程，生成 result/report；有保留题时将题卷/答案写入 run 目录。
5. 必需派生产物均写入成功后，最后写入 `run_finished` 并 flush/fsync；之后仅显示结果位置，不安排其他决定成功与否的必需写入。

JSONL 事件按完整行写入并在以上关键边界 flush/fsync；JSON/Markdown 通过同目录临时文件写入、flush/fsync、单文件 rename。临时文件名称由程序控制，不接受任意写入路径。

不承诺多文件原子事务。失败目录可能保留部分题卷或报告；**文件存在不代表已成功完成**。`result.json` 记录 generation outcome、题目和检查事实，不自行宣称 run 已完成。回放必须检查终态事件及它声明的必需文件摘要；报告说明完整性要以此验证为准。

| 终态 | 约定的必需记录与产物 |
| --- | --- |
| completed | manifest、合法事件流及终态、result、report、运行专属题卷和答案 |
| empty | manifest、合法事件流及终态、result、report；不要求题卷/答案 |
| failed / interrupted | manifest、到实际失败阶段为止应有的事件和终态；派生报告/题卷不强制存在，已成功写入的产物可声明并校验 |

终态列出的文件摘要不包含正在写入的 traces 文件本身，避免自引用哈希；事件流按 schema/顺序/阶段关系验证，replay 再计算完整输入日志摘要。终态不能省略上表要求的文件来绕过校验。完整失败记录意味着失败过程可检查，不意味着生成或文件发布成功。

完整 `run_finished` 行写出后，flush/fsync 仍可能报错：live 必须返回非 0，但稍后回放可能实际读到完整标记和所有匹配文件。此时 `trace_integrity: complete` 仅表示**回放时记录和产物自洽**，不证明原进程完成了持久化确认、返回过 0 或经历断电仍一定保留。报告中的 run_status 是记录里的终态，不等于对原 CLI 退出状态的证明；无法观察的退出状态保留未知。不增加另一个所谓“绝对可靠”的 ack 文件来伪造保证。

### 6.2 状态分离

| 维度 | 值与解释 |
| --- | --- |
| `run_status` | `completed` 有最终题且所有必需产物完成；`empty` 无最终题但正常完成；`failed` 捕获到失败；`interrupted` 捕获到用户中断 |
| `generation_outcome` | `not_started`、`failed`、`model_empty`、`all_dropped`、`retained`；缺信息时 null，不能以 0 代替未知 |
| `trace_integrity` | 回放验证为 `complete` 或 `incomplete`；活跃进程可报告 `io_error`，但不能假定该错误能写回故障磁盘 |
| `semantic_status` | 始终为 `not_evaluated`，包括引用合法的题 |

缺终态事件的旧目录，回放的 `run_status` 为 `unknown`、完整性为 `incomplete`，不凭猜测叫作成功、超时或用户中断。一次调用失败后按原策略成功，整次 run 仍可 completed，但失败调用记录不能消失。

正常 trace CLI：completed/empty 返回 0；已捕获失败返回非 0；可处理的 KeyboardInterrupt 返回 130，并尽力写出 interrupted 终态。硬杀/断电不能保证写终态，不承诺 Python 来不及执行的清理动作。

### 6.3 写入与服务错误

- 初始化 manifest 或首条事件失败：输出明确 I/O 错误，非 0 退出，**零 embedding/生成调用**。
- 调用开始事件无法持久化：不发送对应请求；若已有调用，不再启动后续请求。
- 调用后或产物写入失败：不宣布完整成功，不执行后续模型调用；保留已落盘记录，并尽力记录失败。若记录介质已不可写，向 stderr 输出脱敏错误与 run 路径，退出非 0；不能声称失败事件已保存。
- 记录错误使用专门异常（如 `TraceWriteError`），不能被 `_complete()` 内宽泛的模型 fallback 捕获后吞掉并再次调用模型。
- 普通 provider 错误保留当前 fallback/重试策略；异常详情按白名单生成类别和安全摘要，不序列化 HTTP request/headers 或原始 SDK 异常对象。
- 选材、渲染或输出阶段也纳入 run 生命周期；能记录时写明 error stage。捕获异常不改变未开启 trace 路径原有用户提示。

不能保证从满盘恢复写入，也不能一边称 I/O 失败一边保证“全部错误日志都已保存”。未完成目录留供诊断，不自动删除或续写。

## 7 离线回放

`eval replay RUN_DIR` 只读 manifest/events/产物，校验版本、run ID、事件 seq、调用配对、item 引用、终态和文件摘要，重新计算严格引用结果与基础报告。

- 不调用 `get_settings()` 读取 `.env`，不打开 VectorStore，不加载 embedding/LLM 客户端，不访问网络，不依赖原索引或 PDF 仍存在。
- 正常 completed、empty 或完整记录的 failed/interrupted 运行都可以回放；回放命令成功不表示原运行成功。回放报告原样保留原运行状态。
- 缺终态、尾行被截断或中间损坏：读取可验证前缀、标出最后有效 seq 和损坏位置，不跳过坏行后拼出“完整成功”。可生成诊断报告，但命令返回非 0。
- 不支持的 schema version 明确拒绝，不按当前 schema 猜字段含义。按已发生阶段要求材料/请求事件；重复 call/item ID、非法事件顺序或该阶段应有事件缺失，才判为不完整。
- 索引打开前/选材阶段失败或中断可以没有 `materials_selected` 和调用事件；若开始、失败阶段、终态及该阶段必需产物都有效，则 `generation_outcome: not_started` 且 trace 可完整。已经进入调用或出现题目却缺输入/请求记录，不能借早期失败规则放宽。
- 输出到新的 `replays/<replay_id>/`，不改 manifest、traces、原 result/report 或题卷；反复回放同一输入和同版检查器，除回放 ID/时间外结果应一致。
- 回放产物记录 trace 输入摘要和检查器版本，以区分原始记录中的当时检查与重新计算的检查；差异必须展示，不能覆盖历史。
- 只处理约定的相对产物名，拒绝绝对/上跳路径以及越出 run 根目录的符号链接，不从不可信 manifest 指定的任意地址读取文件。

输出目录不可写则给出非 0 错误，不为完成报告修改原始目录内容。输入不受支持时可以只报错误，不必勉强创建一份结构正常的结果。

## 8 基础报告与分母

PR 1 报告至少列：请求题数、外层 attempt 数、真实生成调用数、格式 fallback、错误/重试、有效空结果、schema 成功题数、保留/修复/丢弃数、输入材料身份、原始及最终引用检查、运行与完整性状态。

引用有效率明确分开：pre_policy 的有效题数 / pre_policy 题数；post_policy 的有效题数 / 最终保留题数。多次无效响应不混进 schema 成功题目的分母，而在调用/验证失败计数单列。修复数是保留题的子集，不与保留数相加来假造总数。

空分母用 NA；schema 尚未成功时原始题数未知，不把它填为 0。缺失 validation、只观察到 provider error 或调用意图时，不推断整次生成已失败；只有捕获生成耗尽且明确写入失败终态才标记 `generation_outcome: failed`。不完整回放的 `count_scope: validated_prefix` 表示计数只覆盖已验证事件前缀。模型明确返回空 items 与所有题被引用策略丢弃，虽然都无题卷，必须用不同 outcome 表达。不能把“调用/生成失败”混成“模型认为材料不适合出题”。

报告不包含 judge 分数、不声称证据支持、不更新 golden 的 human_verified。题目中的 Markdown/HTML/链接作为不可信展示内容处理，不能令生成内容伪装成报告状态标题或可执行页面脚本；本批只生成静态本地报告。

## 9 验收标准

每个 AC 必须有 fixture/离线测试证据，不能用一次真实运行替代。

| 编号 | 必须证明的行为 |
| --- | --- |
| AC-01 | 无 trace 保持行为/文件兼容；开启 trace 使用唯一 run 目录，不覆盖课程历史题卷 |
| AC-02 | 落盘材料顺序、每次 messages/config 与假客户端实际收到的一致，包含修复提示；初始记录先于任何服务调用 |
| AC-03 | response-format fallback 和外层重试各自正确计数，每次客户端调用有唯一 call ID，零额外模型调用；start 落盘但尚未调用即中断时，不冒充已确认的调用数 |
| AC-04 | 完整、空字符串、schema 无效、部分流响应、provider 错误、重试耗尽均保留准确记录，无推测性内容 |
| AC-05 | 严格引用检查正确覆盖跨页、空洞、未知文件、非法页码、重复 chunk；相同题文本也能稳定关联 pre/post |
| AC-06 | 既有 keep/±2 修复/drop 行为不变，修复前无效与修复后有效分别可见，未借机改变 tie-break |
| AC-07 | 初始化、中途事件、单文件写入、终态提交失败均有故障注入；初始失败零服务调用，TraceWriteError 不被模型 fallback 吞掉；终态可读但 fsync 失败时 live 非 0，replay 不谎称原进程成功 |
| AC-08 | model_empty、all_dropped、失败、可捕获中断和缺终态分别处理；早期失败允许无材料/调用且记录完整，已进入调用则不能缺请求；live 确认持久化后才宣布完成 |
| AC-09 | replay 不读 `.env`/索引/模型，不联网、不修改原记录；拒绝未知版本/坏事件/越界路径，损坏前缀可诊断，正常重复回放结果一致 |
| AC-10 | 配置/服务错误不泄漏 key、URL 凭证和 header；reasoning 字段未落盘，usage 不可用为 null |
| AC-11 | Golden、索引、prompt、生成策略和依赖锁文件不因实现而改变；离线新测试及现有测试、lint 有实际执行证据 |
| AC-12 | 报告按正确分母区分 pre/post、未知与空结果；无语义成绩，未完成状态不会被报告成成功 |

## 10 决策与后续边界

本 spec 推荐的工程决策是：opt-in trace、run 专属产物、追加事件、正常终态提交标记、记录失败不静默降级、严格检查不改变产品修复策略、完全离线 replay。这些使 PR 1 不依赖 judge 选择，也不需要用户重新标注。

Judge 型号、预算、语义评分协议与校准方式留给 PR 2 spec，不阻塞本批离线实现。真实模型运行另行确认，不把安装依赖或跑单元测试当作真实模型调用授权。

维护规则：行为或字段改变先更新本 spec；实施步骤与证据只更新 plan；代码、测试和文档在同一批变更中同步。状态依次为草案、实施中、已验证；只有 AC 测试及独立复核证据齐全才标“已验证”。本轮 AC-01–AC-12 已通过离线测试及独立复核；具体环境、命令与限制见 plan 的执行证据台账。

## 11 实现落点与使用

- `evaluation/live.py`：显式 trace 生命周期；只读索引身份、材料快照、产物和终态。
- `evaluation/contracts.py`、`trace.py`：Pydantic 边界、版本与持久化；独立产物失败和事件流失败分开处理。
- `evaluation/citations.py`：严格引用检查；`report.py`：只做事实汇总。
- `evaluation/replay.py`：版本/关系/计数/摘要验证、有效前缀及离线报告。
- `query/generator.py`：可选 recorder；`cli.py`：两个显式入口。

本机原 `.venv` 保留不动；当前可用的隔离入口是 `/private/tmp/overfit-eval-trace-venv/bin/overfit`。
生成命令会使用现有 embedding/LLM 配置，尚未运行；回放仅需已有 run 目录：

```text
/private/tmp/overfit-eval-trace-venv/bin/overfit mock --course IFN580_machine_learning --questions 3 --trace
/private/tmp/overfit-eval-trace-venv/bin/overfit eval replay /Users/silver/Documents/github/Overfit/outputs/eval/<run_id>
```

回放输出中的 `run_status` 是已记录终态，`trace_integrity` 是记录完整性，两者要一起看。
例如 `failed / complete` 表示“失败过程记录完整”，而不是生成成功；`completed / incomplete`
可能是产物缺失或摘要不符。回放返回 0 只表示回放验证成功，原进程退出状态始终不据此推定。
