# PR1 Eval Trace 实施计划

> 状态：PR1 已实现并通过独立离线验收；2026-10-05。
> 已完成 spec、代码、离线测试及证据记录。未调用真实模型，未执行 commit/push。

规范：[EVAL-TRACE-SPEC.md](/Users/silver/Documents/github/Overfit/docs/specs/EVAL-TRACE-SPEC.md)。
背景：[EVALUATION.md](/Users/silver/Documents/github/Overfit/docs/EVALUATION.md)。
schema、事件名、字段、终态和验收 ID 以 spec 为准；本计划组织实施顺序，不另立一套协议。

## 1. 本 PR 交付边界

实现一条可离线检查的证据链：

```text
显式 mock --trace
  → 在任何索引/embedding 调用前建立运行记录
  → 保存实际材料、准确消息和每次 chat create 的可见输出
  → 保存引用处理前后题目及稳定关联
  → 严格 source/page 检查与基础报告
  → eval replay RUN_DIR 纯离线重放
```

两个 CLI 入口均已实现；真实生成 pilot 未执行，当前证据全部来自离线 fixture。

包含：trace 持久化、纯代码引用检查、离线 replay/report、fixture 单元测试和假客户端集成测试。
不包含：独立 LLM judge、语义支持/可作答性判分、MCQ answerer、golden 检索 benchmark、
真实模型 pilot、CI 自动模型调用、重建索引或重新标注 golden。

约束：

- 不加 `--trace` 时维持既有产品行为，不产生 eval 目录。
- 启用 trace 后，题卷/答案保存在本次 run 目录，沿用原文件 basename 保持答案链接有效，
  不覆盖默认 outputs 中的课程文件。
- 不修改 prompt、选材顺序/算法、采样参数、重试策略、response-format 降级顺序。
- 不修改现有 ±2 页引用修复/丢弃策略；严格 grader 是旁路观察，不替代产品策略。
- 等距最近页的修复选择保持现状，不顺手确定化；需要改变时另作技术决策与 PR。
- 不引入运行时依赖，不读写 golden、不修改索引，不把 trace 写入课程语料。
- 继续使用已有模型配置；本 PR 不选择 judge 型号，不增加“测试用真实模型”要求。

## 2. 已阅读代码与接入点

| 文件 / 接入点 | 实施前行为 | PR1 接入 |
| --- | --- | --- |
| [/Users/silver/Documents/github/Overfit/src/overfit/cli.py](/Users/silver/Documents/github/Overfit/src/overfit/cli.py) 的 `mock` | 读取 settings、打开索引、选材、生成、直接写两个 Markdown；空结果退出 0 | 增加显式 trace 分支，在 `_open_store` 前建立记录；统一处理成功、空、失败、中断与发布边界 |
| 同文件 `_open_store` | 获取 embedder 并探测维度，再打开 VectorStore | 不改现有行为；证明 run 开始事件早于这里，不让 replay 进入这里 |
| [/Users/silver/Documents/github/Overfit/src/overfit/query/generator.py](/Users/silver/Documents/github/Overfit/src/overfit/query/generator.py) 的 `mock_exam` | 渲染真实 prompt，调用 `_complete`，随后修复/丢弃引用 | 在真实 prompt 与材料边界记录快照，在产品过滤前后记录结构化结果 |
| 同文件 `_complete` | outer attempt 中可多次降级调用；格式失败后加入最多 2,000 字符 assistant 回复和纠错消息 | 每次调用记录当时实际 messages，不用初始 prompt 冒充后续请求；attempt 与 call 数分开 |
| 同文件 `_stream` | 真实 `chat.completions.create` 与流式消费；返回拼接 content；另计 reasoning 更新 | 紧贴真实调用分配 call ID，保存完整/部分可见 content；不保存 reasoning 内容 |
| 同文件 `_drop_invented_citations` | 近页修复、未知来源/远页丢弃，返回字符串 dropped 列表 | 添加可观察处理结果的接口，不改变选择规则；稳定关联不能靠字符串或内容匹配 |
| [/Users/silver/Documents/github/Overfit/src/overfit/config.py](/Users/silver/Documents/github/Overfit/src/overfit/config.py) | `get_settings()` 会加载环境与 `.env`，`outputs_dir` 会解析为绝对路径 | live run 使用已有设置并显式白名单提取安全字段；replay 不读取/构造 Settings |
| [/Users/silver/Documents/github/Overfit/src/overfit/models.py](/Users/silver/Documents/github/Overfit/src/overfit/models.py) | `Chunk` 有 `source/page/page_end`；`GeneratedExam` 允许显式空 items | 使用实际 chunk 元数据；不能把 schema-invalid 响应归为合法空结果 |

### 已实现模块边界

| 实现路径 | 职责 |
| --- | --- |
| `/Users/silver/Documents/github/Overfit/src/overfit/evaluation/contracts.py` | spec 的版本化 manifest/event 边界数据契约 |
| `/Users/silver/Documents/github/Overfit/src/overfit/evaluation/live.py` | 显式 trace 生命周期、索引身份、产物发布与异常终态 |
| `/Users/silver/Documents/github/Overfit/src/overfit/evaluation/trace.py` | 唯一运行目录、manifest、事件 writer、故障传播 |
| `/Users/silver/Documents/github/Overfit/src/overfit/evaluation/citations.py` | 不联网、不修复的严格引用纯函数 |
| `/Users/silver/Documents/github/Overfit/src/overfit/evaluation/replay.py` | 只读取既有运行文件、验证事件完整性、重算结果 |
| `/Users/silver/Documents/github/Overfit/src/overfit/evaluation/report.py` | 从派生结果渲染报告；无生成器/Settings 依赖 |

具体文件拆分允许调整，但离线依赖边界不能弱化。新模块不得在 import 时初始化模型、
读取 `.env` 或打开索引；不要为了复用现有 `render_exam()` 引入配置和当前日期副作用。

## 3. 实施里程碑

以下为实施核对表；已完成项按离线测试与独立复核勾选，证据见第 6 节。

### P0 · 冻结规范、保护环境与工作区

- [x] 审阅 spec 中事件顺序、状态/完整性双轴、I/O 失败规则、发布边界和验收 ID。
- [x] 保存实施前 `git status`，区分既有未提交文档与本 PR 新变更；不清理或覆盖用户文件。
- [x] 记录解释器检查：当前 `/Users/silver/Documents/github/Overfit/.venv/bin/python` 指向
  `/root/.local/share/uv/python/cpython-3.12-linux-aarch64-gnu/bin/python3.12`，是本机不可用目标。
- [x] 实施时先建立独立临时开发环境，例如 `/private/tmp/overfit-eval-trace-venv`；保留原
  `.venv`，不原地重建。已按锁文件安装 Python 3.12.14 独立环境。
- [x] 显式使用独立环境运行测试；避免普通 `uv run` 隐式修复/替换仓库原 `.venv`。
- [x] 使用纯人工构造 fixture，不复制真实日志中的密钥、材料或模型输出。

退出条件：规范可实施；测试环境与原环境隔离；未增加真实模型调用预算。

### P1 · 契约、事件 writer 与最小 fixture

- [x] 按 spec 建立 schema 版本和事件验证；拒绝不支持的版本，不默默当作最新版。
- [x] 所有机器产物带 schema_version 1 与一致 run ID；事件 seq 从 1 严格递增，UTC
  RFC 3339 时间用于审计、单调时钟用于耗时。落盘边界使用已有 Pydantic。
- [x] 在 `settings.outputs_dir / "eval" / run_id` 用随机 UUID 排他创建唯一目录，碰撞换新 ID，
  不覆盖/续写旧记录；可复用 task ID 与一次性 run ID 分开。
- [x] 先持久化初始 `manifest.json` 与开始事件，再允许索引/embedding/生成操作。
- [x] `traces.jsonl` 按序追加事件；保留已成功写入记录，不在失败时重写为成功日志。
- [x] 实现 spec 规定的 flush/写入确认边界。明确断电仅保证已经成功持久化的完整记录
  可读，不承诺所有内存 delta 已保存或整套文件具备数据库事务性。
- [x] 区分运行状态 `completed/empty/failed/interrupted` 与完整性：replay 验证为
  `complete/incomplete`，活跃进程可以报告 `io_error`；不假设 I/O 错误能写回故障磁盘。
  失败调用也可能具有完整记录。
- [x] 硬 kill/断电导致缺少终态时，replay 给 `run_status: unknown` 与 incomplete，
  不把“没有完成事件”自行推断成已捕获的 interrupted。
- [x] 写入失败抛专用异常；初次持久化失败阻止 embedding/模型调用，调用后写失败阻止
  后续调用和最终发布成功声明；未提交目录中的残留文件不算完整产物。
- [x] 使用安全字段白名单：不 dump 完整 Settings、环境、HTTP header、SDK 对象或异常原文。
  endpoint 身份去掉 credentials/query 等敏感部分；异常按 spec 脱敏。
- [x] 创建最小成功、空结果、失败、截断尾行 fixture，供后续模块先行开发。

退出条件：不接入模型也能验证事件与写失败策略；尚不声称业务路径已经被记录。

### P2 · 真实调用边界的可观察性

- [x] 增加可选 recorder/observer，未启用时为 no-op，不改原有返回值与异常行为。
- [x] 在 `mock_exam` 中保存传入 chunk 的完整文本、ID、顺序、source、page/page_end，
  section 和缺省 null，以及真实渲染的 system/user 内容；校验材料页范围，不静默修复
  元数据。不重新 gather/retrieve 生成“近似输入”，不为了节省空间裁剪实际内容。
- [x] 选材事件记录实际索引路径、meta，以及同一读取事务获得的 documents hash 清单与
  摘要；不为了初始化 manifest 提前探测 embedding，也不把该摘要称为全量 SQLite 快照 SHA。
- [x] 在 `_complete` 的每次 `_stream` 入口保存该次实际消息、response_format 与调用配置。
  记录与发送共享同一冻结快照，避免后续消息列表修改污染历史；后续纠错消息按实际
  截断结果保存，准确记录空 response-format 为 `null`。
- [x] 将唯一 `call_id` 对应到每次真实 `client.chat.completions.create`，包括降级调用；
  outer attempt 只代表原有验证重试轮数，不作为请求总数。
- [x] `call_started` 只证明调用意图已持久化，不证明客户端方法已进入或服务端已收到。
  加入 start 写成后、真正 create 前硬 kill 的 fixture：分别报告 start/finished/unresolved
  数，存在孤立 start 时精确 `call_count` 为 null，不拿 start 数推算已调用次数或费用。
- [x] 在 `_stream` 收集可见 content，正常结束或可捕获异常时通过 `call_finished` 保存
  完整/部分响应与 `response_complete`；不要求逐 token 落盘。创建请求失败、流中断、
  KeyboardInterrupt 等分支保留实际已收到的可见内容，不填造内容；硬 kill 未落盘片段
  允许丢失但必须体现完整性限制。
- [x] 不读取/序列化 reasoning 内容到 trace；已有 UI reasoning 更新计数不能冒充 token usage。
- [x] usage 不可获得时显式为 null，不能用 delta 更新次数估算并冒称服务计量。
- [x] 在 capability fallback 的 `except Exception` 前显式传播 recorder/I/O 失败，
  不把本地写盘故障误判为“端点不支持 json_schema”后继续发送。
- [x] 保留原有 schema extraction/coercion、反馈截断、尝试次数、温度与降级顺序。

退出条件：正常闭合路径中假客户端方法调用次数与 trace 一致；不完整路径的调用次数保持
未知而非推测。记录实际消息，无 reasoning/凭证；不把 fake 调用当作线上服务调用证据。

### P3 · 原始/最终题目关联与严格引用检查

- [x] 保存 schema 验证成功、产品引用处理之前的题目快照；原始 raw response 另存，不混同。
- [x] 以 spec 的稳定 item ID 和原序号建立 pre/post mapping；重复题目也必须分别关联，
  不能用问题文本、答案文本、citation 字符串或最终列表位置猜测原题。
- [x] 产品引用处理提供逐题 kept/repaired/dropped 观察记录；保留现有修复/丢弃行为与次序。
- [x] 对原始及最终题目分别运行严格校验：source 精确相等，page 必须为正整数且落入该 source 的某个
  实际 chunk 区间 `[page, page_end or page]`；不接受 basename、模糊路径、±2 容错或跨空隙范围。
- [x] 原始引用被产品修复后有效，报告仍显示原始失败、最终有效、发生修复。
- [x] 丢弃题保留稳定 item ID，post_policy 为 null，post 检查为 `not_applicable`；不能计为通过。
- [x] 缺失/非法页码、非法 source、不可解析响应、无有效题目分别处理；不伪造题目去扩大分母。
- [x] 产品显式空数组与过滤后全丢弃的空结果分别保留原因；空内容/格式失败不算合法空数组。
- [x] 报告明确引用有效只证明输入中有该路径/页范围，不代表材料支持答案或题目可作答。

退出条件：只用 fixture 即可复查原始错误、修复、丢弃与最终状态，不改变产品出题结果。

### P4 · CLI 生命周期、失败与发布边界

- [x] 为 `mock` 增加 `--trace`，默认关闭。非 trace 路径保持既有输出位置、空结果退出与提示。
- [x] 仅 trace 路径在任何服务调用前校验 `questions > 0`、`material >= 0`；0 的材料预算
  仍用既有计算规则，不顺手改变未开启 trace 的无效参数行为。
- [x] trace 打开后覆盖 `_open_store`/embedding/selection 阶段的异常；即使尚未生成，也保存
  已开始运行及失败阶段，不只为成功模型响应建日志。
- [x] 收拢异常/中断的终结逻辑，避免 `typer.Exit`、KeyboardInterrupt 或 finalizer 重复写终态。
- [x] trace 路径的题卷/答案写入 run 目录，保持原 basename；不写默认课程输出文件。
- [x] 先用临时文件 + 单文件 rename 写入 `result.json`、`report.md` 及运行专属题卷/答案，
  最后写入并 flush/fsync `run_finished` 作为提交标记；仅当全部必需文件与提交标记成功
  才声明完成。按 spec 区分 empty/failed 情况各自必需的文件，不强求失败运行有题卷。
- [x] 按 spec 6.1 固定必需产物：completed 要 result/report/题卷/答案，empty 要
  result/report；failed/interrupted 只强制 manifest、到失败阶段应有事件及终态，
  已写派生产物可声明并校验。不能靠终态漏列文件绕过 required 集合。
- [x] `run_finished` 的文件摘要不包含正在写入的 traces 本身，避免自引用哈希；
  replay 在读取后另算日志输入摘要，不引入额外 ack 文件声称绝对持久化保证。
- [x] 注入上述每个边界的写入失败；不承诺跨文件事务原子性。失败目录可留部分文件，
  文件“存在”不代表已完整发布；不得在提交标记前打印成功。
- [x] `result.json` 只保存 generation_outcome 与检查事实，不独立宣称 run 完成；报告
  与 replay 的运行完成判断以日志终态和必需文件完整性为准。
- [x] 遇到日志 I/O 故障终止进一步模型调用和最终发布成功声明、返回非零；如果连错误
  事件也不能写入，仅保留已写数据、stderr 报告失败，不能声称错误事件已保存或持久化
  已确认。普通 artifact 写失败尽力记录 failed 事件，但不掩盖再次写入失败。
- [x] 单独注入完整终态行已经写出、随后 flush/fsync 失败：live 非零且不宣布成功；
  稍后 replay 若读到自洽终态及全部匹配文件，可给 trace_integrity complete。它只证明
  当前记录自洽，不证明原 CLI 的 fsync 确认或 exit 0，未知退出状态保持未知；不能强制
  此边界 replay 一律 incomplete。
- [x] 保留 `result.json` 与 `report.md` 为本轮派生产物；不得在 replay 中覆盖它们。
- [x] 输出清晰的 run 目录与终态：completed/empty 退出 0，捕获失败非零，可处理的
  KeyboardInterrupt 退出 130；失败不打印成功结论，输出发布错误按 spec 单独呈现。

退出条件：live mock 的四类运行终态和 I/O 失败均有可检查证据；未用真实模型验证。

### P5 · 纯离线 replay 与报告

- [x] 新增 `eval replay RUN_DIR` 子命令，仅从显式目录读取 manifest/events。
- [x] replay 不调用 `get_settings()`，不构造 Settings，不读取 `.env`，不打开 VectorStore，
  不 import/初始化模型客户端；CLI 注册和导入也不触发这些操作。
- [x] 校验 schema_version、run ID、严格递增 seq、调用配对、唯一 call/item ID 与事件顺序。
  不支持的版本明确拒绝，不用当前 schema 猜读。损坏尾部/中部只保留可验证前缀，记录
  最后有效 seq 和损坏位置；不跳过坏行继续拼出成功。仅在已发生阶段要求材料/请求事件，
  不能把早期索引/selection 失败的合法记录误判成缺事件。
- [x] 索引打开前或选材阶段捕获 failed/interrupted 时，可没有材料/请求事件；若开始、
  失败阶段、终态与该阶段必需产物有效，generation_outcome 为 not_started，trace 可完整。
  已进入调用或已有题目却缺输入/请求的记录仍须拒绝为完整，加入两组对照 fixture。
- [x] 按 spec 核验终态声明的必需产物摘要。缺少终态时
  运行状态为 unknown，不能凭 `result.json` 内容补造 completed/interrupted。
- [x] 以 trace 为事实来源重算严格引用结果；不从原 `result.json` 复制结果冒充回放。
- [x] 在 `RUN_DIR/replays/<replay_id>/` 创建唯一派生目录；保存规范要求的输入身份与
  回放版本，重复执行不覆盖既有 replay，更不修改原 manifest/events/result/report。
- [x] 只读取约定的相对产物名；拒绝绝对/上跳路径和越出 run 根的符号链接，不能按
  不可信 manifest 读取任意文件。replay 输出目录不可写时非零退出，不改原记录补救。
- [x] 完整记录的 failed/interrupted 可成功回放，但保留原运行失败状态；缺终态或损坏
  前缀可输出诊断报告，命令必须非零。未知版本可只报错，不勉强创建结构正常的结果。
- [x] 保存 trace 输入摘要与检查器版本，展示重算结果与历史检查差异，不覆盖原判定。
- [x] 报告展示运行状态、完整性、请求数/outer attempts、解析情况、pre/post 引用结果、
  修复/丢弃、异常和未评语义维度。零分母为 N/A，不输出 100% 或误导性零分。
- [x] 同一 trace 与同一评分代码应得到一致的内容结果；回放 ID/时间等审计元数据可以不同。
- [x] 不完整输入只给“已观察到”的局部计数与限制；未观察到的事件不填成已确认的零。
- [x] pre 有效率以 schema 成功题数为分母，post 以最终保留题数为分母；无效响应另计，
  重复 chunk 不增加题目分母，修复数是保留数的子集。尚未 schema 成功时原题数未知，
  不填成 0；全部语义字段均为 `not_evaluated`。
- [x] 将题目中的 Markdown/HTML/链接当作不可信显示数据，进行安全转义/隔离，防止伪装
  报告状态标题或成为可执行脚本；只生成静态本地报告。

退出条件：把 settings、模型和索引入口全部替换为会报错的哨兵，replay 仍能通过 fixture 测试。

### P6 · 回归、故障注入与独立审阅

- [x] 运行原有 parser 测试与新增 eval/generator/CLI 测试，保存真实命令、环境和结果。
- [x] 运行 Ruff 与 `git diff --check`；检查锁文件、prompt、golden 和索引均未变化。
- [x] 对无 `--trace` 路径做行为回归：相同 fake 输入产生相同题目、修复/丢弃和产品文件内容。
- [x] 用独立 reviewer 检查 spec 到实现/测试的验收映射、失败路径和数据边界；作者不自批通过。
- [x] 复查 trace 中无凭证/reasoning、离线 replay 无网络/配置读取，且完整性声明不超出证据。
- [x] 只有验收满足后才将文档状态改为已实现；尚未做的真实模型 pilot 留给后续授权阶段。

## 4. 测试矩阵与验收映射

已实现测试全部使用 fixture、monkeypatch 和 fake streaming client；不需要 embedding/LLM 服务。
AC ID 对应主 spec；以下表格给出测试文件映射。每项均以 fixture 验证；spec 后续修订时同步此表。

| AC | 测试组 | 必须覆盖的例子 | 证据位置 |
| --- | --- | --- | --- |
| AC-01 | 兼容/选择性 trace | 不加 flag 不建 eval 目录；加 flag 题卷/答案仅写 run 目录，默认课程文件 SHA 不变 | `/Users/silver/Documents/github/Overfit/tests/test_eval_cli.py` |
| AC-02 | 实际材料/请求 | chunks 文本与顺序、跨页 metadata、冻结 messages/config 与 fake client 实收一致；初始记录先于全部服务 | `/Users/silver/Documents/github/Overfit/tests/test_generator_trace.py` |
| AC-03 | Call capture | 首轮三档降级各有 ID；第二轮仅原有两档；outer attempt 与调用数分离；start 落盘/create 前硬 kill，start/finished/unresolved 分列且精确 call_count=null | 同上 |
| AC-04 | Partial/异常 | 完整响应、空字符串、schema 无效、创建失败、流中断及部分 content、格式重试耗尽，均不推测内容 | 同上及 CLI 测试 |
| AC-05 | 严格引用/mapping | 单页/跨页/区间空隙、同 basename 不同目录、未知 source、无效页、重复 chunk；重复题独立关联 | `/Users/silver/Documents/github/Overfit/tests/test_eval_citations.py` |
| AC-06 | 产品策略不变 | ±2 原始失败但产品修复；远页/未知来源丢弃；等距页不改策略；pre/post 无误配 | 同上及 generator 测试 |
| AC-07 | Writer/提交/I/O | 唯一目录/碰撞；初次失败零服务调用；partial 保存失败不降级；产物 rename 失败；终态写/flush/fsync 失败 live 不成功；完整终态行后 fsync 失败的自洽 replay 不误称原 CLI exit 0；保留前缀 | `/Users/silver/Documents/github/Overfit/tests/test_eval_trace.py` 及 CLI 测试 |
| AC-08 | Lifecycle | 早期索引/selection failed/interrupted 可完整且 not_started、无材料/请求；显式空、全丢弃、格式失败、成功、中断与硬 kill 缺终态分开 | `/Users/silver/Documents/github/Overfit/tests/test_eval_cli.py` |
| AC-09 | Replay | 无配置/模型/索引/网络访问；不覆盖；坏前缀/未知版本/重复 ID/坏 seq/越界路径拒绝；早期失败无材料合法 vs 已进入调用缺材料不完整；缺终态/必需产物；原 SHA 不变 | `/Users/silver/Documents/github/Overfit/tests/test_eval_replay.py` |
| AC-10 | Secret/reasoning | 假 API key、带凭证 endpoint、header、异常字符串、reasoning 均不泄漏；可见 content 保留；usage 缺失为 null | writer/generator/replay 测试 |
| AC-11 | 边界/回归 | golden、索引、依赖、prompt 不变；既有 parser 与全部离线测试；禁止 socket/真实模型入口 | CLI/generator 和现有测试 |
| AC-12 | Reporting | pre/post 分母、N/A、未知/失败分开、partial 局部计数、语义未评、提交状态限制、同输入内容可重复 | replay/report 测试 |

## 5. 已验证的测试入口与后续使用

以下命令已在独立环境实际运行；原仓库 `.venv` 未改动：

```bash
cd /Users/silver/Documents/github/Overfit
/private/tmp/overfit-eval-trace-venv/bin/python -m pytest -q
/private/tmp/overfit-eval-trace-venv/bin/ruff check src tests
git diff --check
```

以下 CLI 已实现并通过 fake-client 端到端测试；真实 `mock` 会使用 embedding 与生成
模型，真实 pilot 尚未执行：

```text
/private/tmp/overfit-eval-trace-venv/bin/overfit mock --course IFN580_machine_learning --questions 3 --trace
/private/tmp/overfit-eval-trace-venv/bin/overfit eval replay /Users/silver/Documents/github/Overfit/outputs/eval/<run_id>
```

trace 的真实根目录来自 `settings.outputs_dir`，上面路径仅代表仓库默认 outputs 配置。
replay 的输入是已有 run 目录，不根据当前 settings 猜目录或重新调用生成器。

## 6. 执行证据台账

### 2026-10-05 实施与独立验收

| 内容 | 实际证据 / 限制 |
| --- | --- |
| 代码基线 | HEAD `9e68aa9` + 当前工作区变更；既有文档与 golden 未清理；未 commit/push |
| 环境 | `/private/tmp/overfit-eval-trace-venv`；Python 3.12.14；`uv sync --locked` 成功，未改 pyproject/uv.lock |
| 原环境 | `.venv/bin/python` 仍指向 `/root/.local/share/uv/python/cpython-3.12-linux-aarch64-gnu/bin/python3.12`，保留未替换 |
| 全套测试 | `/private/tmp/overfit-eval-trace-venv/bin/python -m pytest -q` → **187 passed**（包含原有 5 个 parser 测试） |
| 静态检查 | `/private/tmp/overfit-eval-trace-venv/bin/ruff check src tests` → All checks passed；`git diff --check` 通过 |
| CLI 表面 | `overfit mock --help` 显示 `--trace`；`overfit eval replay --help` 显示 `RUN_DIR`；未调用真实服务 |
| 黄金集 | `eval/ifn580_eval.json` SHA256：`60d1455702a17df25119d4232b3ee2c95cda493cb255f4811d14de0107cb5a5f`，与实施前一致 |
| 不变边界 | prompt、retriever、依赖定义/锁文件及原 parser 测试无差异；本轮没有写索引、运行 ingest 或真实模型 |
| 网络防线 | `tests/conftest.py` 全局禁止 socket connect/create_connection/connect_ex；生成使用 synthetic fake client |
| 独立复核 | `eval_doc_verifier` 只读复核 AC-01–12，并独立重跑 187 tests、Ruff、diff 检查；无阻塞问题 |

### 里程碑与证据文件

| 里程碑 | 实现 / 测试证据 | 结果 |
| --- | --- | --- |
| P0 环境与规范 | spec v0.2、独立锁定环境、初始工作区记录 | 完成 |
| P1 writer/contracts | `tests/test_eval_trace.py`：版本/UTC、碰撞、安全配置、真实 replace/fsync 故障与 fail-closed | 完成 |
| P2 generator capture | `tests/test_generator_trace.py`：请求快照、格式降级/重试、partial content、usage/reasoning、异常隔离 | 完成 |
| P3 citations/mapping | `tests/test_eval_citations.py` 与 generator tests：区间空洞、非法页、重复题 ID、±2 与原策略一致 | 完成 |
| P4 CLI lifecycle | `tests/test_eval_cli.py`：默认兼容、早期失败/中断、四种产物失败、fake live→replay | 完成 |
| P5 replay/report | `tests/test_eval_replay.py`：损坏前缀、未知版本、终态计数/摘要、只读/路径、未知状态、转义、重复回放 | 完成 |
| P6 全套验收 | 187 passed + Ruff + diff + 独立 reviewer | 完成；仅离线范围 |

### 复核发现与修复

1. 非 dict chunk 导致损坏日志回放崩溃 → 边界转为 ValueError，保留有效前缀。
2. 无 validation 或仅 provider error 被误判成生成失败 → 未知 outcome 保持 null；只有捕获生成耗尽的失败终态明确 failed。
3. 单个 artifact 失败导致遗漏可写的失败终态 → `artifact_failed` 与事件 `failed` 分离；前者只允许 best-effort 失败/中断终态，后者不得续写。
4. 增补终态计数字段比对、`count_scope`、多行报告文本隔离及真正 terminal fsync 故障的回放测试。

验收不等于：真实服务集成已经运行、断电耐久性已经验证、题目语义质量达标或 judge 已校准。
所有故障为 fixture 注入；尚未实施的 judge、真实 pilot 与后续 golden 检索/MCQ runner 不在本轮勾选范围内。

## 7. 停止条件与后续决策

- spec 的发布顺序、I/O 失败或恢复规则若在实现时无法一致满足，先提交设计问题，不能
  用 broad except、覆盖日志或悄悄放宽 complete 来“跑通”。
- 独立环境无法按锁文件安装时先记录技术阻塞，不改旧 `.venv`、不升级依赖追最新。
- PR1 不受 judge 型号/预算决定阻塞；后续 PR 接入 judge 前才冻结模型、调用上限和语义协议。
- 等距修复确定化、完整 SQLite 快照实验、golden 划分与 MCQ 路径各自独立决策，不塞进本 PR。
