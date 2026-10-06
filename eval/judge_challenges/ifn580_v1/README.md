# IFN580 judge 人工挑战样本 v1

这是一份**独立的人工设计 challenge**：复用现成的三题和生成时的六个材料 chunks，包含 **3 个未改 control + 4 个单因素 mutant，共 7 条**。不是新的真实生成 trace，不是正式 golden set，也不是模型执行结果或人工校准结论。

## 文件

- `inputs.json`：原始六个 chunks、七条输入，以及仅供审计的来源路径、SHA-256 和 ID。
- `annotations.json`：control 配对、人工变体类型、完整 field-level before/after、修改理由和定性检查目标。**不允许投喂给 judge。**
- 本 README：范围、泄漏隔离和后续使用边界。

## 样本设计

| Case | 对应 control | 唯一修改因素 | 主要观察维度 |
| --- | --- | --- | --- |
| case-001 | 自身 | 原题1逐字保留 | 配对基线，不预设通过 |
| case-002 | 自身 | 原题2逐字保留 | 配对基线，不预设通过 |
| case-003 | 自身 | 原题3逐字保留 | 配对基线，不预设通过 |
| case-004 | case-001 | 答案中一条信息损失论断反转 | 材料／引用支持 |
| case-005 | case-002 | 答案中“检查过拟合”改成“无需检查” | 材料／引用支持 |
| case-006 | case-003 | 只换引用 source/page 为现有但无关页 | 引用支持，不应混同材料支持 |
| case-007 | case-002 | 只改题干，要求未提供的候选模型具体准确率和最高者 | 可作答性／数据不足 |

source/page 是同一个引用因素的两个字段。所有变体共享原六个 chunks，材料文本、字段值及顺序均未修改。case-007 的原答案有意保持不变，以隔离题干修改；原文另一页的 epoch 训练日志不能被当成该场景中各候选模型的对照准确率。

`annotations.json` 中的定性目标是人工设计意图，不是模型结果；不硬定整体必须是 `partially_supported` 或 `unsupported`。原 control 只是原样复制，不因其身份就自动成为“正确答案”真值。

## 独立执行入口（已实现并独立离线验收）

**数据集与 `judge-challenge` 独立执行入口均已独立离线验收（519 tests）。真实 suite 已完成零调用 prepare，但尚未执行真实 challenge 模型调用，也未完成语义校准。**

规范与进度见 [challenge spec](/Users/silver/Documents/github/Overfit/docs/specs/EVAL-CHALLENGE-SPEC.md) 和 [challenge plan](/Users/silver/Documents/github/Overfit/docs/plans/EVAL-CHALLENGE-PLAN.md)。

不能直接把此文件夹传给 `overfit eval judge RUN_DIR`：该命令要求真实完整生成 trace。不得创建假 manifest、伪造事件或校验和来绕过 trace 完整性校验。新命令 `judge-challenge` 使用独立记录类型，不借用正式 judge 的 trace 身份。

执行适配器必须逐条选取案例，并且只发送：

- 可作答性阶段：该条 `input.question` + 六个共享 `chunks`，不得泄漏答案、最终引用或 topic；
- 支持阶段：该条 `input.question`、`input.answer`、最终 `input.source` / `input.page` + 六个共享 `chunks`；
- 不发送 `annotations.json`、本 README、case ID、control/mutant 身份、provenance、修改理由、预期目标或其他案例。

因此不能将整个 `inputs.json` 原封不动序列化成模型输入。保留 `topic` 是为了完整复制原题，不代表需要将它加入当前两个 judge 阶段的请求。


### 命令与报告

以下命令在项目根目录执行；工程验收证据见 plan：

```bash
# 默认纯离线 prepare，不读取模型配置或联网
uv run overfit eval judge-challenge eval/judge_challenges/ifn580_v1

# 明确选择模型、端点和预算后，由用户手动执行；7 cases × 2 phases
JUDGE_MAX_ATTEMPTS=1 uv run overfit eval judge-challenge \
  eval/judge_challenges/ifn580_v1 --execute --max-calls 14
```

可用 `--max-items 1 --max-calls 2` 小规模接通（只选 case-001，不是完整 control/mutant 对比）；`--max-items` 按案例顺序选前 N 条，不自动补配对 control。未选案例始终保留在报告中。默认每阶段最多 2 次尝试，预算包含重试；上例 JUDGE_MAX_ATTEMPTS=1 显式关闭 runner 重试，以免把 14 次预算误当保证每阶段都成功。

默认在当前工作目录的 `outputs/eval/challenges/<UUID>/` 输出；`--output-dir PATH` 改变父目录。报告给出原始模型三维结论、技术完成、人工待复核和 control/mutant 配对变化，人工设计目标仅在本地展示，**不自动算检测成功率或正确率**。输入/注释原字节快照、hash、可见请求响应、usage 与终态独立保存，不覆盖本目录或历史正式评审。

已生成的 [离线 prepare 报告](/Users/silver/Documents/github/Overfit/outputs/eval/challenges/47c16ac8-faed-450b-ae21-6acf7365272e/report.md) 为 prepared、7 cases、0/14 已评阶段、0 次调用；不是模型判断。

本轮不提供 challenge replay/resume；不要将输出传给正式 `eval judge-replay`，其应拒绝不同记录类型。保全记录是为了可审计与未来扩展，不代表已有回放入口。prepare 和离线测试不是模型实验结果，模型结论也不是人工校准真值。

## 保全与审计

来源为 `outputs/eval/1f5d21a7-c742-43f2-9287-9b832379906a/judgments/b32e3a2e-0db7-40ad-9d48-fc80d525db1c/tasks.json`，绝对路径和文件 SHA-256 记录于 `inputs.json.provenance`。

此副本不修改原 outputs、golden、索引、judge 代码或 `.env`。校验只证明数据复制与单因素修改符合设计，不能证明 judge 已能识别这些差异。
