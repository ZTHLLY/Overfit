# Judge Challenge 实施计划

> 日期：2026-10-06 · 状态：已实现并通过独立离线验收（519 tests）；真实 suite 离线 prepare 完成，未在线执行或完成语义校准。
> 本轮授权文档后编码；不调用真实模型，不修改数据或旧结果，不 commit/push。

行为以 [EVAL-CHALLENGE-SPEC.md](/Users/silver/Documents/github/Overfit/docs/specs/EVAL-CHALLENGE-SPEC.md) 为准。仅实现独立 challenge 执行入口，不扩成评测框架。

## 1 实施顺序

- [x] C-P0：先核对现有 7-case/6-chunk 数据，冻结 CLI、信息隔离、记录类型、预算与非目标。
- [x] C-P1：严格 suite loader，整个输入/注释一一配对、before/after 和单因素校验；错误在模型配置和客户端创建前拒绝。
- [x] C-P2：复用既有 judge client/rubric/validator，显式 adapter 隔离字段；prepare 完全离线，execute 显式预算、选中范围与固定重试。
- [x] C-P3：独立 UUID 输出、输入/注释快照和 hashes、意图与可见响应、完整终态及中断/写失败处理。
- [x] C-P4：独立 challenge 指标与逐条/配对报告、人工队列；设计目标与模型结果分开，不自动算检测成功或准确率。
- [x] C-P5：CLI 和 fake-client/故障测试，覆盖 C-01～C-09；现有全量 pytest、Ruff 与 diff check 回归。
- [x] C-P6：独立 reviewer 审查、必要修复与复验；按最终证据同步 suite README、主 README 与 EVALUATION 使用说明。

## 2 文件边界

新模块容纳 loader/runner/report，不修改正式 judge 的 `judge.py`、`runner.py`、`metrics.py`，以保留 checker_identity。CLI 只增入口，不改变正式 judge/replay 行为。tests 可新增 fixture/假客户端/CLI覆盖。input/annotations、.env、golden、索引和历史 outputs 保持不变。

正式 judge-replay 不接受 challenge 新记录类型；不添加 challenge replay/resume。本批不做自动预期标签匹配、缓存、并行、人工裁决录入或新数据生成。

## 3 实现与独立离线验收证据（2026-10-06）

- 新增独立 challenge 模块与 CLI，严格 suite loader 在配置／客户端前校验全部案例；复用已有 judge-v3 请求与响应验证，不更改正式 judge 的 rubric 或指标语义。
- `.venv/bin/python -m pytest -q`：作者、主线程和独立 reviewer 均为 **519 passed**；主线程最终 **1.76 秒**，独立 reviewer **1.93 秒**。`.venv/bin/python -m ruff check src tests`、`git diff --check` 均通过。
- 独立 reviewer 另完成 **8 项禁网络边界检查**：mutant 排在前面而 control 未选时不补调用、6 类坏后缀在配置前拒绝、`1e999` 来源 schema 版本数值溢出拒绝（provenance.source_tasks_schema_version）。配对报告中未执行 control 保持未评，不推断基线结论。
- 主线程在 `socket.connect`／`connect_ex`／`getaddrinfo` 封锁下执行真实 suite 的 CLI 离线 prepare，成功生成 [prepare 报告](/Users/silver/Documents/github/Overfit/outputs/eval/challenges/47c16ac8-faed-450b-ae21-6acf7365272e/report.md)：**prepared、7 cases（3 controls + 4 mutants）、0/14 已评阶段、0 次调用**。
- reviewer 核验该 prepare 的完整产物 hashes、inputs/annotations 原字节快照一致、14 个阶段未评、0 call intent、空人工队列。这只验证集成准备流程，不是 challenge 模型实验。
- **90 个 baseline 文件哈希未变**，包含 inputs/annotations、历史产物和正式 judge 的 judge.py／runner.py／metrics.py。对原正式 v3 记录 `b32e3a2e-0db7-40ad-9d48-fc80d525db1c` 只读调用 `runner._reconstruct`，6/6 阶段兼容性验证通过；没有写回历史记录或生成新语义结论。
- 对 C-01～C-09 的独立代码复核无遗留阻塞；主实现与独立审阅分开进行。输入快照保全和工程测试不证明人工设计目标必被模型识别。
- 本轮真实 challenge 请求数 **0**，未修改 .env、golden 或索引，未在线执行、未完成语义校准，未 commit/push。challenge replay/resume、自动预期命中率、准确率与人工裁决录入仍未实现。

## 4 用户后续运行

离线 prepare 随时可用；实际模型执行需用户自行确认当前独立 JUDGE 配置与明确预算。7 cases 无重试需要 14 次调用；默认每阶段最多 2 次尝试，完整示例设置 JUDGE_MAX_ATTEMPTS=1 禁用 runner 重试。预算包含重试，不承诺 max-calls 14 一定完成所有阶段。只选前 N cases 不会按配对关系自动加案例，报告保留未选 control 的缺失信息。

下一步是观察模型面对配对变体是否给出合理解释，不是让程序代替人宣告 judge 已达标。少量样本表现不能冒称语义校准或生产可靠性。
