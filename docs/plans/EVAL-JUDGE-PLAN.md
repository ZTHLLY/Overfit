# PR2 Eval Judge 实施计划

> 日期：2026-10-06 · 状态：judge-v3 职责修订已实现并通过独立离线验收（439 tests）；v1/v2 验收保留为历史证据，未做在线 v3 pilot，真实模型校准未完成。
> 用户授权本批文档后继续写代码；不调用真实模型，不执行 commit/push。

行为规范以 [EVAL-JUDGE-SPEC.md](/Users/silver/Documents/github/Overfit/docs/specs/EVAL-JUDGE-SPEC.md) 为准，背景见 [EVALUATION.md](/Users/silver/Documents/github/Overfit/docs/EVALUATION.md)。本计划只组织实施和证据，不重复发明标签、分母或运行状态。

## 0 当前 judge-v3 职责修订（2026-10-06）

用户随后完成 OpenRouter 真实试跑；4096 输出预算解决截断，最新完整响应仍被 v2 的省略号定位门槛归零。用户明确要求：语义结论由 judge 给出，程序记录与路由，疑点由人工处理。当前实现以新版 spec 为准；下文 v1/v2 全部为历史行为，不能继续作为 v3 验收要求。

- [x] V3-P0：先更新 spec／plan，冻结模型结论、技术完成、人工复核三层职责；定位为辅助注释、v3 协议和旧记录边界。
- [x] V3-P1：模型 quote 可选；完整结构即完成，未知/partial/矛盾/实质来源疑点进入独立 human_review，raw verdict 不变。
- [x] V3-P2：聚合和报告取消自动确认通过；显示选中覆盖、全量覆盖、三维模型原判与人工队列；pending 不令技术运行 incomplete。
- [x] V3-P3：持久化 review_queue；replay 离线重新派生，不信旧结果；旧 v1/v2 拒绝且不覆盖。
- [x] V3-P4：新增 V3-01～V3-08 离线测试与全量回归；仅对保存响应做纯函数诊断，不请求模型、不写历史输出。
- [x] V3-P5：独立 verifier 审查、修复与复验；依据实际证据回填 spec／plan／README／ARCHITECTURE／EVALUATION。

本轮保持 .env 的 4096 输出配置，不读取或修改密钥，不加载本地模型、不联网调用 judge、不改 golden/索引/生成 prompt/依赖，不 commit/push。review_queue 是人工待办导出，不伪造“已裁决”；人工录入、自动抽检及 UI 暂不实现。


### v3 实现与独立离线验收证据（2026-10-06）

- 实现交付：judge-v3 原模型结论、技术状态、人工状态独立；可选 quote 仅做定位诊断，实质来源／一致性问题和模型不确定性进入人工队列；删除自动确认通过与 admissibility／review 最终裁决字段。模型原始 decoded shape 保留，不用默认值伪造模型输出。
- 聚合与 CLI／报告显示选中/全量覆盖、三维原判、模型全正向（非批准）、技术错误与独立人工待复核；选中任务完整即技术 completed，pending 不使运行 incomplete。阶段级 review_queue.jsonl 与其他产物纳入完整性记录和离线复算。
- `.venv/bin/python -m pytest -q`：代码作者、独立 reviewer、主线程均为 **439 passed**；主线程最终用时 **1.60 秒**。`.venv/bin/python -m ruff check src tests` 与 `git diff --check` 均通过。
- 独立 reviewer 另做 **17 项边界离线检查**，审查本次 V3-01～V3-08 契约与受影响回归边界；最终无阻塞项。摘要中的测试数不是在线模型表现。
- 对已保存真实记录 `54670453-1db1-453f-b63e-6cdad5be1223` 的两个可见响应，仅使用新验证器做只读纯函数诊断：**选中技术完成 2/2，全量覆盖 2/6，模型三维全正向 1 题，人工 pending 0**；两条省略号摘录为 info，未逐字核验但不抹掉模型判断。
- 上一项不是旧协议 replay、不是按 v3 prompt 调用模型，也不是语义正确性校准；历史 judgment／report 均不改写。v3 replay 继续明确拒绝不兼容的 v1/v2 协议和实现身份。
- 独立保全检查：预先记录的 **65 个 baseline 文件**未变化。没有修改历史生成/评审结果，.env 的 4096 输出配置保持不动。
- 本轮新增真实模型调用数 **0**，未加载本地模型、未做在线 v3 pilot、未新增依赖、未 commit/push。人工裁决录入工作流仍未实现；review_queue 只是待办导出，不声称任何人已作裁决。

## 0.1 历史 judge-v2 修订（2026-10-06）

用户已完成本地真实试跑：27B 运行过慢后中断；8B 可返回，但暴露空白差异导致证据失败、模型总判与 claim 冲突等问题。这不是题目不合格的证明，也不是 judge 已完成校准。本轮不启动／加载 Ollama，不改模型配置，不联网调用模型，不执行 commit/push。

新增工作按以下顺序执行；下文 P0–P4 和 371 tests 是 **judge-v1 历史验收**，不可套用为本次完成证据。

- [x] V2-0：先更新 spec／plan，冻结“模型语义结论、证据核验、可采纳性”分层职责和旧协议处理。
- [x] V2-1：结构解析与语义一致性核验分离；保存模型原判，具体原因可见；冲突不按 schema 错误重试。
- [x] V2-2：exact 优先，仅空白折叠定位可回映原文；保留 submitted quote／原文 quote／span／匹配方式，拒绝歧义与改写。
- [x] V2-3：跨页／引用不符不覆盖原判；报告展示模型说了什么、证据能否定位、为什么不采纳；不改变通过门槛和分母。
- [x] V2-4：rubric 明确假设、作答要求与知识断言；协议更新 `judge-v2`，旧协议 replay 明确拒绝，不静默转换旧失败。
- [x] V2-5：fake-client、纯函数、离线 replay、报告及全量回归测试；原始 trace／历史 judgment 文件不修改。
- [x] V2-6：独立 verifier 对 J-14～J-17 及受影响 J-01～J-13 复核；依据实际结果回填文档与测试数量。

新增测试必须覆盖：

- 可解析但结论冲突时保留原 verdict、标明问题、无追分重试、不计通过；格式错误与证据问题清晰分离。
- 换行／tab／多空格、精确原文偏移、多个候选位置、纯空白和未知 chunk；标点／字词变化不得通过。
- 跨页限制保留模型原判；单维可采纳性、有效覆盖及固定分母正确。
- 新版 replay 从可见响应重建三层结果，不信旧派生结果；v1 明确不兼容且旧文件不变。
- prompt 中明确假设／作答指令边界；测试只验证约束文本和请求隔离，不宣称证明模型遵循语义 rubric。

### 历史 v2 实现与独立离线验收证据（2026-10-06）

- 已读取实现确认：`judge.py` 使用 judge-v2，结构 schema 不再直接裁决语义一致性，`validate_review` 输出 model_review／verification／仅 accepted 时非空的 review；runner 保存 raw_response 和独立阶段状态；报告与 CLI 明确 needs_review 不等于内容错误。
- `.venv/bin/python -m pytest -q`：作者、主线程与独立 verifier 均为 **404 passed**；主线程最终 1.36 秒，独立 verifier 1.35 秒。`.venv/bin/python -m ruff check src tests` 与 `git diff --check` 均通过。独立复核无遗留阻塞。
- 复核期间修复 replay 未核验 checker_identity 的问题：现在 judge.py／runner.py／metrics.py 三文件哈希均须一致，并补 3 项回归测试。
- 独立 verifier 在 socket 禁用条件下追加验证 49 组 Unicode 空白的原文 span、重叠重复摘录的歧义拒绝、4 类非空白改写拒绝，以及实际旧运行 949ca 的 v1 replay 明确拒绝。另核对 27 个原 trace／历史 judge 产物 SHA-256 不变。
- 回归 fixture 位于 `/Users/silver/Documents/github/Overfit/tests/fixtures/judge_local_pilot.json`，取自用户已保存本地试跑的最小可见材料／响应，不产生新模型调用。
- 主线程对旧运行 `949ca097-a871-4bec-934e-e926c72a34a9` 的可见响应做只读纯函数诊断：answerability 在新定位器下 accepted，两条 whitespace 映射为原文 `[263,649)`／`[653,912)`；support 的证据 valid，但一致性仍为 needs_review，原因为 material_aggregate_inconsistent。
- 上一项仅证明新校验逻辑如何解释已有响应，**不是旧协议 replay、不是新模型 pilot，也不是将旧运行改判通过**。旧响应没有收到新 rubric；旧 run／judgment 产物保持不变。
- 主线程复查 `/private/tmp/overfit-judge-v2-input-hashes.json` 中 28 个 golden／索引／依赖／原 trace／旧 judge 产物 SHA-256，全部不变。
- 本次模型调用数为 **0**，未启动或加载 Ollama，未修改模型配置，未执行 commit/push。测试证明分层协议和工程行为，不证明 judge 语义准确度；真实 v2 pilot 未运行，校准未完成。

## 1 历史 PR2 目标

```text
已完成 mock trace
  → PR1 离线完整性核验
  → 提取最终保留题和实际材料
  → 默认准备 / 显式执行独立 judge
  → 无答案 answerability + 带答案 support 两任务
  → 输出 schema 和短摘录定位校验
  → 逐题三维结果、固定分母指标、人可读报告
  → 独立记录的离线复算
```

不改生成过程，不重新选材，不读取 golden 当 judge 答案，不改变原 trace 的 semantic_status。PR 2 的新结果与 PR 1 证据通过 run/item 身份连接，而不是把原记录重写成“已评”。

本批不会启动用户真实三题 trace 的在线审查。可以只读验证其结构，或者以 prepare 创建旁路未评报告；不能把先前允许调用生成服务理解成已确认 judge 型号和预算。

## 2 模块边界与接入点

| 路径 | 职责与约束 |
| --- | --- |
| `/Users/silver/Documents/github/Overfit/src/overfit/evaluation/replay.py` | 复用 PR1 `inspect_run`；必要的提取接口不削弱原完整性校验 |
| `/Users/silver/Documents/github/Overfit/src/overfit/evaluation/judge.py` | 独立配置、两个隔离请求、输出 schema、证据定位；仅 execute 初始化客户端 |
| `/Users/silver/Documents/github/Overfit/src/overfit/evaluation/runner.py` | judge 输入提取、固定顺序、全局预算、独立记录与离线复算 |
| `/Users/silver/Documents/github/Overfit/src/overfit/evaluation/metrics.py` | 纯函数聚合固定保留题分母，空分母、超量生成、错误覆盖 |
| `/Users/silver/Documents/github/Overfit/src/overfit/cli.py` | 新增 `eval judge` / `eval judge-replay` 入口，打印状态与具体报告路径；不改变 PR1 命令 |
| `/Users/silver/Documents/github/Overfit/tests/` | 新 judge fixture、假客户端、持久化故障、指标和 CLI 测试 |

文件可按实现调整；模块不得在 import 时加载 `.env`、初始化客户端、打开索引。复用 Pydantic 和现有 SDK，不为本批增加框架／依赖。SDK 行为以锁定版本本地签名及官方资料核对，不猜 API 参数。

## 3 历史 judge-v1 实施里程碑

### P0 规范与工作区保护

- [x] 阅读总体方案第 4、6、12 节及 PR1 契约、汇总与 replay 接口。
- [x] 先定义独立任务、证据边界、正向判定、预算和分母，不以能返回一段 judge 文本为完成。
- [x] 冻结 CLI 标志、独立配置键、输出文件名及版本边界，详见 spec 第 2、4、5 节；实现后再核对一致性。
- [x] 保存 git status、golden 哈希、依赖与 prompt 状态，不回滚既有用户改动。
- [x] 验证当前可用的项目测试环境；不擅自替换用户已修复的 `.venv`。

退出条件：代码作者和 reviewer 都能从 spec 判断预期行为；没有真实请求预算被消耗。

### P1 输入与纯函数协议

- [x] 从完整 PR1 trace 提取 final items、chunks、引用历史与 run 身份；失败／不完整输入拒绝。
- [x] 实现 strict Pydantic 响应结构，不把布尔值当合法数字版本，不接受额外未定义字段。
- [x] 定义两个独立消息构造器及 rubric 版本／hash，answerability 不含答案。
- [x] 实现 evidence chunk／短摘录精确定位和确定性字符区间。
- [x] 实现跨页降 unknown；完整材料支持与具体页支持不混为一谈。
- [x] 单元测试 J-01、J-03、J-04、J-05，先不需要任何客户端。

退出条件：合成正例、材料缺口、无效证据和跨页样本能得到明确且可复验的状态。

### P2 受控执行与审计记录

- [x] 独立 JUDGE 配置，必填 MODEL／BASE_URL／API_KEY，无 fallback；prepare 不读取线上配置。
- [x] 执行入口显式开启且必须 --max-calls 正整数预算，配置错误在客户端调用前退出。
- [x] 客户端 SDK 重试关闭，runner 统一计数；全局预算覆盖所有题与两个任务。
- [x] 固定传输／结构错误重试策略，默认每任务 max_attempts=2；无 format fallback；有效负判与 unknown 不重试。
- [x] 请求输入过大、响应截断、服务错误与预算耗尽明确记状态。
- [x] 每次调用前记录实际冻结请求，随后记录可见响应、usage 和校验结果。
- [x] 使用唯一审查目录和文件写入保护；写失败停止调用，必要终态最后写。
- [x] 脱敏配置和错误，不保存内部 reasoning，不把 raw provider error 写入记录。
- [x] fake-client 故障注入验证 J-02、J-06、J-07、J-08、J-09。

退出条件：从调用记录和假客户端调用次数能证明预算不越界，失败不会被当内容通过。

### P3 汇总、报告和 CLI

- [x] 实现固定保留题分母、全维正向通过、产出率封顶及未知成本。
- [x] 准备报告显式未评，逐题保留 not_evaluated，不把 0 通过解释成 100% 质量失败。
- [x] 输出面向人的中文摘要、逐题题干／答案／证据与限制，机器 JSON 保持稳定字段。
- [x] CLI 显示报告绝对路径；错误／未完成状态与内容负判区分退出码。
- [x] 离线从 call_finished.response.content 重新校验 schema／证据并聚合，校验 source 身份／序列／hash，不信任派生 judgments 或旧 metrics。
- [x] 多次执行／复算不覆盖原 trace 或旧判断，拒绝路径穿越和 symlink。
- [x] 用网络禁用 fixture 验证 prepare 和 replay，覆盖 J-10、J-11、J-12。

退出条件：用户不用读 JSONL 就能看清“哪些题、哪一维、什么证据、哪些没评”，工程上又可追溯每次调用。

### P4 独立验收与文档回填

- [x] 全量离线 pytest 和 Ruff，保留命令、结果、测试数量与环境证据。
- [x] 独立 reviewer 对照 J-01 至 J-13 检查实现，作者不得自批通过。
- [x] 修复阻塞项并跑相关／全量回归，收集 reviewer 复核结论。
- [x] 根据实际代码更新 spec 的接口、产物和延期项；按证据而不是预期勾选计划。
- [x] 同步总方案和 README：PR2 能力已实现与真实 pilot 未进行分开写。
- [x] 复查 golden、索引、生成 prompt、依赖锁和 PR1 原始 trace 未被修改。
- [x] 最终交付命令、文档路径、测试证据及真实 judge 前仍需确认的模型／预算。

退出条件：无已知阻塞项，测试通过，独立复核完成；不执行 commit/push。

## 4 最小测试矩阵

| 类别 | 关键样本 | 对应验收 |
| --- | --- | --- |
| 输入 | 完整三题、重复题干不同 item ID、空题集、缺终态、错误源 hash、已丢弃题 | J-01、J-09 |
| 调用隔离 | answerability 请求中只存在答案字段的敏感哨兵不可见（不禁止材料天然含答案）；support 允许答案；上次审查结果不串入 | J-03 |
| Schema | 正常三维、额外字段、缺字段、错枚举、类型 coercion、空响应、无效 JSON | J-04、J-07 |
| 证据 | 正确短摘录、错 chunk、子串不存在、重复短摘录、空摘录、伪造偏移 | J-04 |
| 跨页 | 仅跨页 chunk、同页单页 chunk、其他页有依据而引用页无依据 | J-05 |
| 重试预算 | 首答负判不重试、unknown 不重试、证据错误不重试、格式失败可重试、全局耗尽、SDK 不隐式重试 | J-06 |
| 错误 | 超时、输入超限、响应截断、全部错误、一维成功一维预算耗尽 | J-07、J-11 |
| I/O | 初始写失败、call intent 写失败、响应写失败、终态缺失、损坏中间行 | J-08、J-09 |
| 安全保存 | URL凭证／query、错误对象中的 secret、reasoning 字段、不可信 HTML／Markdown | J-08、J-12 |
| 聚合 | 全通过、部分支持、unknown、错误、未评、零保留、超量保留、每维覆盖 | J-11 |
| 离线 | prepare／replay 禁网络禁 Settings／索引；篡改旧 metrics；源不变，多次复算唯一目录 | J-02、J-10 |
| 回归 | 全部 PR1 单元／CLI 测试，普通未开启 trace 的 mock 行为 | J-13 |

## 5 历史 judge-v1 执行证据（2026-10-05）

2026-10-05，主实现与独立 reviewer 分别完成验收。J-01 至 J-13 无遗留阻塞。测试只使用 fake client、合成材料和临时目录，不调用真实 judge。

### 环境和命令

- PR1 历史测试曾使用隔离 Python 3.12.14；用户随后通过 `uv run` 修复项目 `.venv`，当前为 Python 3.12.13。本轮直接使用当前环境，无新依赖安装。
- `.venv/bin/python -m pytest -q`：**371 passed**；独立 reviewer 重跑结果相同（1.14 秒）。
- `.venv/bin/python -m ruff check src tests`：通过。
- `git diff --check`：通过。
- `uv run overfit eval judge --help` 与 `judge-replay --help` 对应 CLI 参数已实际核对；正式跑分命令仍需独立配置和显式预算。

### 实际交付和独立复核

- `judge.py`：显式独立配置、SDK retries=0、两个隔离消息构造器、严格 schema、定位后的证据、跨页限制和一次可见调用适配。
- `runner.py`：完整 PR1 输入校验、全保留题清单、可选前 N 题、全局预算、独立目录、calls/task 事件、派生判断与完成标记；离线从原始 response content 重验。
- `metrics.py`：完整保留题分母、状态优先级、产出率封顶、未知 usage、中文摘要及逐题证据；首屏显示运行状态、记录完整性和同源偏差。
- CLI：`eval judge RUN_DIR` 默认 prepare；`--execute --max-calls N` 才进入模型调用；`eval judge-replay JUDGE_DIR` 离线重算。incomplete 返回 1，interrupted 返回 130，不因内容负判返回运行错误。
- 独立复核覆盖 J-01～J-13。修复并回归了 prepare 将 task_finished 误当模型调用、replay incomplete/interrupted 退出状态、允许重试的错误提前终结记录、非有限数字拒绝、same_model 元数据复算，以及跨页限制逐题呈现。
- 请求/响应与材料存在不保证语义可靠；claims 由 judge 自行枚举，代码不能证明它穷尽所有必要论断。

### 真实 PR1 输入的零调用验证

用户此前运行的源 run：

`/Users/silver/Documents/github/Overfit/outputs/eval/1f5d21a7-c742-43f2-9287-9b832379906a`

该 run 最终保留 3 题，PR1 replay 为 `completed / complete`。本轮只运行 PR2 prepare 及离线 replay：

- prepare 目录：`/Users/silver/Documents/github/Overfit/outputs/eval/1f5d21a7-c742-43f2-9287-9b832379906a/judgments/b70f0854-0a55-4309-93d3-ff7b2269e154`
- 新 replay 目录：上述目录下的 `replays/edbbb158-2e22-4a78-95d6-a9c6736f4edf`。
- 两者均 **0 judge calls、3 保留题、0 / 6 审查任务已评**，状态 prepared / complete；报告可读，不把未评当内容不合格。
- 独立 reviewer 禁用网络及 Settings/client 路径再验证已保存 prepare 数据；不修改源生成记录，不用 judge 补判断。
- 以上是集成流程证据，不是三题答案正确或语义受支持的证据。

### 文件保全和边界

主实现前后校验以下 SHA-256 一致：

| 文件 | SHA-256 |
| --- | --- |
| golden `eval/ifn580_eval.json` | `60d1455702a17df25119d4232b3ee2c95cda493cb255f4811d14de0107cb5a5f` |
| 正确索引 `index/IFN580_machine_learning.db` | `668fb3b946af45c0a73da1d398a245767d2f20a20d605d54358410c610b34e4a` |
| `pyproject.toml` | `b3ab9180956851e29fe4b0c459daad69099829c37a4cd2f91735c41011ba5879` |
| `uv.lock` | `5197ebad1ca33270170fbdeadd97cd9a24d762ad2dd05d0b04d4964c95817701` |

生成 prompt 与 PR1 原始文件不变；prepare/replay 只增加独立旁路目录。旧连字符 580 索引已在本轮之前按用户授权删除，当前仅保留下划线版本 36 文档／1,385 chunks；这不是 PR2 重建或改写索引。

本轮真实 judge 请求数为 **0**，没有 commit 或 push。真实 judge 服务、预算与小批校准仍待另行确认，离线验收不等于允许自动联网或付费执行。

## 6 历史 v2 后续计划（当前步骤以第 0 节 v3 为准）

完成本次离线验收后，是否重新试跑仍由用户决定；不因历史已配置模型就自行加载本地模型。若用户同意，先确认实际 judge 模型、端点、上下文能力、超时和调用预算。在现有三题 trace 上固定参数做小批审查，预计无重试上限至少 6 次调用；两次 judge 请求不能被误说成每题一次。

Pilot 必须完整保留负判、unknown 和服务错误；不要挑通过题展示。开发者／助手可先核对证据和争议，但应标注模型复核而不是人工核验。少量校准是否需要用户参与取决于具体边界，不新增全面标注任务。

自动跨 run 缓存、恢复中断、并行、金额硬预算、PDF 页级视觉证据和被丢弃题语义审查均明确延期。若后续添加，先改 spec 与测试，不能悄悄扩大当前“完整评测”的含义。
